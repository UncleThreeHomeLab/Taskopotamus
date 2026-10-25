"""Bounded multipart media upload; no file-sized buffers or redirects."""
import hashlib
import http.client
import json
import re
import secrets
import time
from urllib.parse import quote


class TelegramRejected(RuntimeError):
    def __init__(self, message="Telegram rejected the request.", retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def rejection(data):
    if isinstance(data, dict) and data.get("ok") is False and type(data.get("error_code")) is int:
        delay = (data.get("parameters") or {}).get("retry_after") if isinstance(data.get("parameters", {}), dict) else None
        retry = delay if data["error_code"] == 429 and type(delay) is int and 1 <= delay <= 300 else None
        return TelegramRejected(retry_after=retry)
    return None


def result(data):
    rejected = rejection(data)
    if rejected:
        raise rejected
    if not isinstance(data, dict) or data.get("ok") is not True or "result" not in data:
        raise RuntimeError("Telegram returned an unconfirmed result.")
    return data["result"]


def send_media(token, chat_id, item, info, source, connection_factory=http.client.HTTPSConnection):
    if (type(info.get("size")) is not int or not 0 < info["size"] <= 50000000
            or not re.fullmatch(r"[a-f0-9]{64}", info.get("sha256", ""))):
        raise RuntimeError("Invalid attachment metadata.")
    image = item["type"] == "image"
    if image and (info.get("presentation_mime") not in ("image/png", "image/jpeg") or info["size"] > 10000000):
        raise RuntimeError("Inline image validation unavailable.")
    field, method = ("photo", "sendPhoto") if image else ("document", "sendDocument")
    boundary = "taskopotamus_" + secrets.token_hex(16)
    fields = {"chat_id": str(chat_id), "protect_content": "false", "caption": item.get("caption", "")}
    prefix = b"".join((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
                      for name, value in fields.items())
    # Generated ASCII filename prevents header injection; UTF-8 original remains available to clients.
    extension = ".png" if image and info["presentation_mime"] == "image/png" else ".jpg" if image else ""
    filename = re.sub(r"[^A-Za-z0-9._ -]", "_", info.get("filename", "attachment"))[:150] or "attachment" + extension
    original = quote(info.get("filename", "attachment"), safe="")
    mime = info["presentation_mime"] if image else "application/octet-stream"
    prefix += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"; "
               f"filename*=UTF-8''{original}\r\nContent-Type: {mime}\r\n\r\n").encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    connection = connection_factory("api.telegram.org", timeout=10)
    deadline = time.monotonic() + 90
    try:
        connection.putrequest("POST", f"/bot{token}/{method}")
        connection.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
        connection.putheader("Content-Length", str(len(prefix) + info["size"] + len(suffix)))
        connection.endheaders()
        connection.send(prefix)
        remaining, digest = info["size"], hashlib.sha256()
        while remaining:
            if time.monotonic() >= deadline:
                raise RuntimeError("Attachment transfer exceeded its deadline.")
            chunk = source.read(min(65536, remaining))
            if not chunk or len(chunk) > remaining:
                raise RuntimeError("Attachment stream ended before completion.")
            digest.update(chunk)
            connection.send(chunk)
            remaining -= len(chunk)
        if source.read(1) or digest.hexdigest() != info["sha256"]:
            raise RuntimeError("Attachment bytes could not be verified.")
        connection.send(suffix)
        response = connection.getresponse()
        encoded = response.read(65537)
        if len(encoded) > 65536:
            raise RuntimeError("Telegram returned an excessive response.")
        data = json.loads(encoded)
        if 300 <= response.status < 400:
            raise RuntimeError("Telegram redirect was not followed.")
        return result(data)
    except TelegramRejected:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise RuntimeError("Telegram media delivery could not be confirmed.") from None
    finally:
        connection.close()
