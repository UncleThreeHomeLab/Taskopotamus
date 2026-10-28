"""Download Telegram inputs from one fixed host without exposing credential URLs."""
import hashlib
import re
import tempfile
import time
from http.client import HTTPException
from urllib.parse import quote
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from .media import TelegramRejected
from .response_body import DeadlineExpired, read_chunk


class InputRejected(ValueError):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def download(token, descriptor, api, opener=None, remaining=20000000):
    filename = descriptor.get("filename", "attachment.bin")
    if (
        not isinstance(filename, str)
        or not 1 <= len(filename) <= 200
        or filename in (".", "..")
        or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in filename)
    ):
        raise InputRejected("File name is invalid. Rename the document and send it again.")
    mime = descriptor.get("mime", "application/octet-stream")
    if not isinstance(mime, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.+_-]*/[a-z0-9][a-z0-9.+_-]*", mime):
        mime = "application/octet-stream"
    try:
        info = api(token, "getFile", {"file_id": descriptor["file_id"]})
    except TelegramRejected:
        raise InputRejected(
            "Telegram could not provide this file. Send it again as a document of at most 20 MB."
        ) from None
    path = info.get("file_path") if isinstance(info, dict) else None
    if (
        not isinstance(path, str)
        or len(path) > 1024
        or not re.fullmatch(r"[A-Za-z0-9_/-]+(?:\.[A-Za-z0-9_-]+)?", path)
        or path.startswith("/")
        or any(p in ("", "..", ".") for p in path.split("/"))
    ):
        raise InputRejected("Telegram returned an unavailable file.")
    declared = info.get("file_size")
    if declared is not None and (type(declared) is not int or not 0 < declared <= remaining):
        raise InputRejected("Files exceed 20 MB combined.")
    opener = opener or build_opener(ProxyHandler({}), NoRedirects())
    stream = tempfile.TemporaryFile("w+b")
    try:
        with opener.open(
            Request("https://api.telegram.org/file/bot" + token + "/" + quote(path, safe="/")), timeout=10
        ) as response:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > remaining):
                raise InputRejected("Files exceed 20 MB combined.")
            size, digest, deadline = 0, hashlib.sha256(), time.monotonic() + 60
            while True:
                chunk = read_chunk(response, min(65536, remaining - size + 1), deadline)
                if not chunk:
                    break
                size += len(chunk)
                if size > remaining:
                    raise InputRejected("Files exceed 20 MB or the transfer deadline.")
                stream.write(chunk)
                digest.update(chunk)
            if size <= 0 or declared is not None and size != declared or length is not None and size != int(length):
                raise InputRejected("File download was incomplete.")
        stream.seek(0)
        return {"filename": filename, "mime": mime, "size": size, "sha256": digest.hexdigest()}, stream
    except HTTPError as error:
        error.close()
        stream.close()
        raise InputRejected("Telegram file is unavailable. Please send it again.") from None
    except DeadlineExpired:
        stream.close()
        raise InputRejected("Files exceed 20 MB or the transfer deadline.") from None
    except (URLError, OSError, HTTPException):
        stream.close()
        raise RuntimeError("Telegram file download could not be confirmed.") from None
    except BaseException:
        stream.close()
        raise
