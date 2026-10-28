"""Offline framing regressions using real HTTP parsers and local socket pairs."""
import io
import json
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from http.client import HTTPResponse
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from http_response import SyntheticSocket
from src.telegram.bot import api
from src.telegram.input_files import InputRejected, download
from src.telegram.media import TelegramRejected
from src.telegram.response_body import read_body, read_chunk


@contextmanager
def trickling_response(body, framing, status=200):
    client, peer = socket.socketpair()
    client.settimeout(2)
    peer.settimeout(2)
    stop = threading.Event()
    headers = f"HTTP/1.1 {status} Synthetic\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
    chunk_size = f"{len(body):X}".encode()
    if framing == "extension":
        prefix, suffix = chunk_size + b";slow=", b"\r\n" + body + b"\r\n0\r\n\r\n"
    else:
        prefix, suffix = chunk_size + b"\r\n" + body + b"\r\n0\r\nX-Slow:", b"\r\n\r\n"

    def send():
        try:
            peer.sendall(headers + prefix)
            for _ in range(60):
                if stop.wait(0.02):
                    return
                peer.sendall(b"a")
            peer.sendall(suffix)
        except OSError:
            pass

    thread = threading.Thread(target=send, daemon=True)
    response = HTTPResponse(client)
    thread.start()
    try:
        response.begin()
        yield response
    finally:
        response.close()
        stop.set()
        client.close()
        peer.close()
        thread.join(2)
        if thread.is_alive():
            raise AssertionError("Synthetic peer did not terminate.")


class TelegramFramingTests(unittest.TestCase):
    def test_chunk_extensions_trailers_and_prefetched_bytes_survive_wrapping(self):
        body = b'{"ok":true,"result":{"value":7}}'
        wire = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + b"3;first=extension\r\n" + body[:3] + b"\r\n"
            + f"{len(body) - 3:X};second=extension\r\n".encode() + body[3:]
            + b"\r\n0\r\nX-Trailer: accepted\r\n\r\n"
        )
        with HTTPResponse(SyntheticSocket(wire)) as response:
            response.begin()
            original = response.fp
            self.assertTrue(original.peek())
            original.close = Mock(wraps=original.close)
            deadline = time.monotonic() + 1
            first = read_chunk(response, 2, deadline)
            wrapped = response.fp
            second = read_chunk(response, 2, deadline)
            self.assertIs(response.fp, wrapped)
            self.assertEqual(first + second + read_body(response, 65536, deadline), body)
            self.assertTrue(original.closed)
            wrapped.close()
            original.close.assert_called_once()

    def test_slow_chunk_extensions_and_trailers_bound_success_and_error_bodies(self):
        for framing in ("extension", "trailer"):
            for failed in (False, True):
                body = json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}).encode() if failed else b'{"ok":true,"result":{}}'
                with self.subTest(framing=framing, failed=failed), trickling_response(
                    body, framing, status=400 if failed else 200
                ) as response:
                    original = response.fp
                    error = HTTPError("https://example.invalid/123:synthetic", 400, "private", {}, response)
                    started = time.monotonic()
                    with patch("src.telegram.bot.urlopen", side_effect=error if failed else None, return_value=response):
                        with self.assertRaises(RuntimeError) as caught:
                            api("123:synthetic", "sendMessage", timeout=0.15)
                    self.assertLess(time.monotonic() - started, 0.75)
                    self.assertNotIsInstance(caught.exception, TelegramRejected)
                    self.assertNotIn("synthetic", str(caught.exception))
                    self.assertNotIn("private", str(caught.exception))
                    self.assertTrue(original.closed)
                    self.assertTrue(response.closed)

    def test_slow_download_chunk_framing_closes_original_body_and_temporary_stream(self):
        metadata = Mock(return_value={"file_path": "documents/file.txt", "file_size": 1})
        stream = io.BytesIO()
        with trickling_response(b"a", "extension") as response:
            original = response.fp
            # Shorten the existing 60-second body budget without changing the real socket clock.
            clock = SimpleNamespace(monotonic=lambda: time.monotonic() - 59.85)
            started = time.monotonic()
            with patch("src.telegram.input_files.time", clock), patch(
                "src.telegram.input_files.tempfile.TemporaryFile", return_value=stream
            ):
                with self.assertRaises((InputRejected, RuntimeError)) as caught:
                    download("synthetic-token", {"file_id": "id"}, metadata, opener=Mock(open=Mock(return_value=response)))
            self.assertLess(time.monotonic() - started, 0.75)
            self.assertNotIn("synthetic-token", str(caught.exception))
            self.assertTrue(stream.closed)
            self.assertTrue(original.closed)
            self.assertTrue(response.closed)
