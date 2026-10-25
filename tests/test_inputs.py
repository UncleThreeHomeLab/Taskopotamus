import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from openai_codex import LocalImageInput, TextInput

from database_support import isolated_database
from src.agent.chat_service import ChatService
from src.agent.http_server import AgentHTTPServer
from src.messaging.artifacts import ArtifactStore
from src.telegram.agent_client import AgentRequestError
from src.telegram.bot import process_input
from src.telegram.delivery_store import DeliveryStore
from src.telegram.inbox import Inbox
from src.telegram.input_files import download, InputRejected
from src.test_console.server import ConsoleState, make_handler
from src.test_console.uploads import parse_upload
from src.worker.input_files import materialize_inputs, request_tag, input_name


def multipart(csrf, nonce, text="", files=()):
    boundary = "synthetic-input-boundary"
    result = b""
    for key, value in (("csrf", csrf), ("nonce", nonce), ("text", text)):
        result += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
    for name, mime, content in files:
        result += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
            + content
            + b"\r\n"
        )
    return boundary, result + f"--{boundary}--\r\n".encode()


class InputTests(unittest.TestCase):
    def setUp(self):
        self.database = isolated_database(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.store = ArtifactStore(self.database, Path(self.directory.name) / "artifacts", clock=lambda: self.now)

    def upload(self, operation, data=b"alpha,17\nbeta,23\n", mime="text/csv"):
        return self.store.upload(
            "42",
            "1",
            "input",
            operation,
            "sample.csv",
            mime,
            len(data),
            hashlib.sha256(data).hexdigest(),
            io.BytesIO(data),
        )

    def test_selected_input_native_image_and_expired_cached_replay(self):
        image = io.BytesIO()
        Image.new("RGB", (96, 64), "yellow").save(image, format="PNG")
        picture = self.upload("image", image.getvalue(), "image/png")
        document = self.upload("csv")
        orphan = self.upload("orphan", b"unselected")
        codex, thread, turn = Mock(), Mock(), Mock()
        codex.account.return_value = SimpleNamespace(account={"synthetic": True})
        codex.thread_start.return_value = thread
        thread.id = "thread-input"
        thread.turn.return_value = turn
        turn.run.return_value = SimpleNamespace(
            status=SimpleNamespace(value="completed"), final_response="done", usage=None
        )
        tokens = []

        def factory(profile, token):
            tokens.append(token)
            manifest = self.store.input_manifest(token)
            self.assertEqual(
                [i["artifact_id"] for i in manifest["files"]], [picture["artifact_id"], document["artifact_id"]]
            )
            with self.assertRaises(PermissionError):
                self.store.accepted_input(token, orphan["artifact_id"])
            return nullcontext(codex)

        chat = ChatService(factory, self.database, artifacts=self.store)
        selected = [picture["artifact_id"], document["artifact_id"]]
        self.assertEqual(chat.reply("42", "Inspect these", "1", attachments=selected)["text"], "done")
        supplied = thread.turn.call_args.args[0]
        self.assertIsInstance(supplied[0], TextInput)
        self.assertIsInstance(supplied[1], LocalImageInput)
        data = json.loads(supplied[0].text)
        self.assertEqual(data["attachments"][0]["path"], supplied[1].path)
        self.assertIn("/workspace/.incoming/", supplied[1].path)
        self.now += 4000
        self.store.maintain()
        self.assertEqual(chat.reply("42", "Inspect these", "1", attachments=selected)["text"], "done")
        turn.run.assert_called_once()
        with self.assertRaises(ValueError):
            chat.reply("42", "Inspect these", "1", attachments=list(reversed(selected)))
        with self.assertRaises(ValueError):
            self.store.input_identity("43", "1", selected)

    def test_attachment_only_and_malformed_claimed_image(self):
        malformed = self.upload("malformed", b"<html>untrusted</html>", "image/png")
        with self.assertRaises(ValueError):
            self.store.accept_inputs("42", "1", [malformed["artifact_id"]], 300)
        for value in ([{}], ["x"], [True]):
            with self.assertRaises(ValueError):
                self.store.input_identity("42", "1", value)


class WorkerInputTests(unittest.TestCase):
    def info(self, identifier, data):
        return {"artifact_id": identifier * 32, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    @unittest.skipIf(os.name == "nt", "Requires Linux descriptor permissions")
    def test_atomic_read_only_materialization_retry_and_unsafe_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = b"verified input"
            info = self.info("a", data)
            manifest = {"request_tag": "b" * 64, "files": [info]}
            source = Mock(side_effect=lambda _: io.BytesIO(data))
            kwargs = {"runtime_gid": os.getgid(), "owner_uid": os.getuid()}
            materialize_inputs(manifest, source, root, 100, **kwargs)
            target = root / ".incoming" / ("b" * 64) / input_name(info)
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(target.stat().st_mode & 0o777, 0o440)
            materialize_inputs(manifest, source, root, 0, **kwargs)
            source.assert_called_once()
            os.chmod(target.parent, 0o770)
            with self.assertRaises(ValueError):
                materialize_inputs(manifest, source, root, 100, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".incoming").symlink_to("/tmp")
            with self.assertRaises(OSError):
                materialize_inputs(manifest, source, root, 100, **kwargs)

    @unittest.skipIf(os.name == "nt", "Requires Linux descriptor permissions")
    def test_capacity_and_partial_transfer_never_publish_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = self.info("a", b"correct")
            manifest = {"request_tag": "c" * 64, "files": [info]}
            kwargs = {"runtime_gid": os.getgid(), "owner_uid": os.getuid()}
            with self.assertRaises(OverflowError):
                materialize_inputs(manifest, lambda _: io.BytesIO(b"correct"), root, 1, **kwargs)
            with self.assertRaises(ValueError):
                materialize_inputs(manifest, lambda _: io.BytesIO(b"bad"), root, 100, **kwargs)
            self.assertEqual(list((root / ".incoming").iterdir()), [])


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.database = isolated_database(self)
        self.polling = DeliveryStore(self.database)
        self.now = 1000.0
        self.inbox = Inbox(self.database, clock=lambda: self.now)

    def update(self, identifier, album=None, text="caption", file_id="synthetic"):
        message = {
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "caption": text,
            "document": {"file_id": file_id, "file_name": "notes.txt", "mime_type": "text/plain", "file_size": 3},
        }
        if album is not None:
            message["media_group_id"] = album
        return {"update_id": identifier, "message": message}

    def test_album_membership_interleaving_late_member_and_restart(self):
        self.inbox.receive(self.update(10, "album"), {"42"})
        unrelated = {
            "update_id": 11,
            "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "separate"},
        }
        self.inbox.receive(unrelated, {"42"})
        self.inbox.receive(self.update(12, "album"), {"42"})
        self.assertEqual(self.inbox.offset, 13)
        self.assertIsNone(self.inbox.claim())
        self.now += 2
        job = self.inbox.claim()
        self.assertEqual(job["id"], 10)
        self.assertEqual(len(job["messages"]), 2)
        restarted = Inbox(self.database, clock=lambda: self.now)
        restarted.recover()
        self.assertEqual(restarted.claim()["id"], 10)
        restarted.finish(10)
        self.assertEqual(restarted.claim()["id"], 11)
        restarted.finish(11)
        restarted.receive(self.update(13, "album"), {"42"})
        self.assertIn("already started", restarted.claim()["error"])

    def test_invalid_input_finishes_without_model_and_unknown_inference_does_not_publish_busy(self):
        update = self.update(10)
        update["message"]["document"]["file_size"] = 20000001
        self.inbox.receive(update, {"42"})
        job = self.inbox.claim()
        agent = Mock()
        agent.lookup_reply.return_value = None
        with patch("src.telegram.bot.api", return_value=True):
            process_input(job, self.inbox, {"42"}, agent, "synthetic", self.polling, threading.Event())
        agent.chat.assert_not_called()
        agent.publish_reply.assert_called_once()
        self.assertIsNone(self.inbox.claim())
        self.inbox.receive(self.update(11), {"42"})
        job = self.inbox.claim()
        info = {"artifact_id": "a" * 32, "size": 3}
        self.inbox.save_upload(11, 0, info)
        job["uploads"] = {"0": info}
        agent.reset_mock()
        agent.lookup_reply.return_value = None
        agent.chat.side_effect = AgentRequestError("unknown transport")
        with patch("src.telegram.bot.api", return_value=True), self.assertRaises(AgentRequestError):
            process_input(job, self.inbox, {"42"}, agent, "synthetic", self.polling, threading.Event())
        job = self.inbox.claim()
        self.assertTrue(job["inference_started"])
        agent.chat.side_effect = AgentRequestError("busy", 429)
        with patch("src.telegram.bot.api", return_value=True), self.assertRaises(AgentRequestError):
            process_input(job, self.inbox, {"42"}, agent, "synthetic", self.polling, threading.Event())
        agent.publish_reply.assert_not_called()
        agent.lookup_reply.return_value = {"delivery_id": "confirmed"}
        with patch("src.telegram.bot.api", return_value=True):
            process_input(self.inbox.claim(), self.inbox, {"42"}, agent, "synthetic", self.polling, threading.Event())
        self.assertEqual(agent.chat.call_count, 2)

    def test_download_limits_redirect_path_and_secret_redaction(self):
        api = Mock(return_value={"file_path": "../auth", "file_size": 3})
        with self.assertRaises(InputRejected):
            download("synthetic-token", {"file_id": "id", "filename": "a.txt"}, api)
        api.return_value = {"file_path": "documents/file.txt", "file_size": 3}
        response = io.BytesIO(b"abc")
        response.headers = {"Content-Length": "3"}
        opener = Mock()
        opener.open.return_value = response
        info, stream = download("synthetic-token", {"file_id": "id", "filename": "a.txt"}, api, opener=opener)
        with stream:
            self.assertEqual(stream.read(), b"abc")
        self.assertEqual(info["size"], 3)
        opener.open.side_effect = OSError("https://api.telegram.org/botsynthetic-token/private")
        with self.assertRaises(RuntimeError) as caught:
            download("synthetic-token", {"file_id": "id", "filename": "a.txt"}, api, opener=opener)
        self.assertNotIn("synthetic-token", str(caught.exception))

    def test_persisted_text_job_ignores_legacy_processing_cursor(self):
        self.polling.finish(100)
        self.inbox.receive(
            {"update_id": 10, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "hello"}},
            {"42"},
        )
        agent = Mock()
        agent.chat.return_value = {"text": "ready", "delivery_id": "a" * 32}
        with patch("src.telegram.bot.api", return_value=True):
            process_input(self.inbox.claim(), self.inbox, {"42"}, agent, "synthetic", self.polling, threading.Event())
        agent.chat.assert_called_once_with(42, "hello", "10")
        self.assertEqual(self.polling.offset, 101)

    def test_text_crash_intent_survives_restart_and_busy_cannot_replace_original_success(self):
        from src.messaging.delivery_store import DeliveryStore as Outbox

        class HardCrash(BaseException):
            pass

        outbox = Outbox(self.database)
        self.inbox.receive(
            {"update_id": 10, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "do work"}},
            {"42"},
        )
        agent = Mock()
        paid_starts = []

        def started_before_crash(chat, text, request):
            self.assertTrue(self.polling.uncertain(10))
            paid_starts.append(request)
            raise HardCrash()

        agent.chat.side_effect = started_before_crash
        agent.lookup_reply.side_effect = lambda chat, request: outbox.get(str(chat), request)
        with patch("src.telegram.bot.api", return_value=True), self.assertRaises(HardCrash):
            process_input(self.inbox.claim(), self.inbox, {"42"}, agent, "synthetic", self.polling, threading.Event())

        restarted_store = DeliveryStore(self.database)
        self.assertTrue(restarted_store.uncertain(10))
        restarted_inbox = Inbox(self.database, clock=lambda: self.now)
        restarted_inbox.recover()
        agent.chat.side_effect = AgentRequestError("Original inference is still running.", 429)
        with patch("src.telegram.bot.api", return_value=True), self.assertRaises(AgentRequestError):
            process_input(
                restarted_inbox.claim(), restarted_inbox, {"42"}, agent, "synthetic", restarted_store, threading.Event()
            )
        # The dispatcher requeues exceptions; no notice may occupy the original reply identity.
        restarted_inbox.finish(10, retry=True)
        agent.publish_reply.assert_not_called()
        self.assertIsNone(outbox.get("42", "10"))

        with self.database.connect() as db:
            outbox.publish(db, "42", "10", [{"type": "text", "text": "Original successful result"}])
        original = outbox.get("42", "10")
        with patch("src.telegram.bot.api", return_value=True):
            process_input(
                restarted_inbox.claim(), restarted_inbox, {"42"}, agent, "synthetic", restarted_store, threading.Event()
            )
        self.assertEqual(outbox.get("42", "10"), original)
        self.assertEqual(paid_starts, ["10"])
        self.assertEqual(agent.chat.call_count, 2)
        self.assertIsNone(restarted_inbox.claim())

    def test_first_definite_text_rejection_can_finish_without_uncertain_replay(self):
        agent = Mock()
        for identifier, status in ((10, 401), (11, 429)):
            self.inbox.receive(
                {
                    "update_id": identifier,
                    "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "hello"},
                },
                {"42"},
            )
            agent.reset_mock()
            agent.chat.side_effect = AgentRequestError("Definite admission rejection.", status)
            with patch("src.telegram.bot.api", return_value=True):
                process_input(
                    self.inbox.claim(), self.inbox, {"42"}, agent, "synthetic", self.polling, threading.Event()
                )
            agent.publish_reply.assert_called_once_with(42, str(identifier), "Definite admission rejection.")
            self.assertFalse(self.polling.uncertain(identifier))
            self.assertIsNone(self.inbox.claim())


class ConsoleInputTests(unittest.TestCase):
    def test_stream_parser_requires_auth_before_files_and_complete_boundary(self):
        boundary, body = multipart("csrf", "nonce", "", [("notes.csv", "text/csv", b"alpha,17")])
        fields, files = parse_upload(
            io.BytesIO(body),
            len(body),
            "multipart/form-data; boundary=" + boundary,
            "csrf",
            lambda n: self.assertEqual(n, "nonce"),
        )
        try:
            self.assertEqual(files[0]["stream"].read(), b"alpha,17")
        finally:
            for item in files:
                item["stream"].close()
        with self.assertRaises(PermissionError):
            parse_upload(
                io.BytesIO(body), len(body), "multipart/form-data; boundary=" + boundary, "wrong", lambda _: None
            )
        with self.assertRaises(ValueError):
            parse_upload(
                io.BytesIO(body[:-12]),
                len(body) - 12,
                "multipart/form-data; boundary=" + boundary,
                "csrf",
                lambda _: None,
            )

    def test_actual_multipart_http_attachment_only_and_changed_duplicate(self):
        client = Mock()
        client.lookup_reply.return_value = None
        client.upload_input.return_value = {"artifact_id": "a" * 32}
        client.console_chat.return_value = {"text": "ready", "status": "completed"}
        state = ConsoleState(client)
        server = AgentHTTPServer(("127.0.0.1", 0), make_handler(state))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        nonce = state.nonce
        boundary, body = multipart(state.csrf, nonce, "", [("notes.csv", "text/csv", b"alpha,17")])

        def post(value):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request(
                "POST",
                "/chat",
                value,
                {
                    "Host": "localhost:8088",
                    "Origin": "http://localhost:8088",
                    "Cookie": "console_session=" + state.cookie,
                    "Content-Type": "multipart/form-data; boundary=" + boundary,
                },
            )
            response = connection.getresponse()
            status = response.status
            response.read()
            connection.close()
            return status

        self.assertEqual(post(body), 303)
        import time

        deadline = time.monotonic() + 3
        while state.pending and time.monotonic() < deadline:
            time.sleep(0.01)
        client.console_chat.assert_called_once_with("", nonce, ["a" * 32])
        self.assertEqual(post(body), 303)
        self.assertEqual(post(body.replace(b"alpha,17", b"alpha,18")), 400)
        client.console_chat.assert_called_once()
