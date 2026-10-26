"""Opt-in localhost browser interface; live work starts only from protected forms."""
import os
import re
import secrets
import socket
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote

from src.agent.http_server import AgentHTTPServer
from src.runtime import read_secret
from src.telegram.agent_client import AgentClient, AgentRequestError
from .page import render
from .uploads import parse_upload


_upload_slot = threading.BoundedSemaphore(1)


class ConsoleState:
    def __init__(self, client):
        self.client = client
        self.lock = threading.Lock()
        self.cookie = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.nonce = str(secrets.randbelow(10**20))
        self.jobs = {}
        self.pending = False
        self.transcript = []
        self.auth = {"state": "unknown"}
        self.notice = "Ready. Check sign-in status or send a message."
        self.draft = ""
        self.details = None
        self.uncertain = None
        self.attachments = {}
        self.active_request = None
        self.reply_view = None

    def snapshot(self):
        with self.lock:
            return {key: getattr(self, key) for key in
                    ("csrf", "nonce", "pending", "transcript", "auth", "notice", "draft", "details", "uncertain")}

    def _show_messages(self, view, items, metadata):
        if not isinstance(items, list) or not items:
            return
        rendered = []
        for item in items:
            if item.get("type") in ("image", "file"):
                info = metadata.get(item.get("artifact_id"), {}) if isinstance(metadata, dict) else {}
                if info.get("state") == "committed":
                    self.attachments[item["artifact_id"]] = info
                rendered.append({**item, "metadata": info})
            else:
                rendered.append(item)
        if view["position"] is None:
            self.transcript = (self.transcript + [("You", view["text"]), ("Agent", rendered)])[-20:]
            view["position"] = len(self.transcript) - 1
        else:
            self.transcript[view["position"]] = ("Agent", rendered)
        visible = {item.get("artifact_id") for _, entry in self.transcript if isinstance(entry, list) for item in entry}
        self.attachments = {key: value for key, value in self.attachments.items() if key in visible}
        view["count"] = len(items)

    def refresh_messages(self):
        with self.lock:
            view = self.active_request
            if view is None or view["terminal"]:
                return
        try:
            result = self.client.snapshot_reply("console:local", view["id"])
        except Exception:
            return
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            return
        with self.lock:
            if self.active_request is view and not view["terminal"] and len(result["items"]) >= view["count"]:
                self._show_messages(view, result["items"], result.get("artifacts", {}))
                view["terminal"] = result.get("reply_status") in ("completed", "failed")
                view["failed"] = result.get("reply_status") == "failed"

    def check_upload_nonce(self, nonce):
        with self.lock:
            if nonce != self.nonce and nonce not in self.jobs or self.pending:
                raise ValueError("Stale or busy upload.")

    def submit(self, action, nonce, text="", uploads=()):
        signature = (
            (action, text)
            if not uploads
            else (action, text, tuple((i["filename"], i["mime"], i["size"], i["sha256"]) for i in uploads))
        )
        with self.lock:
            if nonce in self.jobs:
                previous = self.jobs[nonce]
                if previous != signature:
                    raise ValueError("Submission changed.")
                return
            if nonce != self.nonce or self.pending:
                raise ValueError("Stale submission. Refresh the page.")
            if action == "chat" and (not text.strip() and not uploads or len(text) > 16000):
                raise ValueError("Message must contain 1 to 16000 characters.")
            if action == "replay" and self.uncertain is None:
                raise ValueError("No uncertain request.")
            replay = self.uncertain
            self.jobs[nonce] = signature
            while len(self.jobs) > 64:
                del self.jobs[next(iter(self.jobs))]
            self.nonce = str(secrets.randbelow(10**20))
            self.pending = True
            self.notice = "Processing. This page refreshes while work is running."
            self.draft = text if action == "chat" else self.draft
        threading.Thread(target=self._run, args=(action, nonce, text, replay, uploads), daemon=True).start()
        return True

    def refresh_status(self):
        try:
            result = self.client.status()
            self._auth(result)
        except Exception:
            with self.lock:
                self.notice = "Sign-in status unavailable. Check the agent service and try Refresh sign-in status."

    def _auth(self, result):
        if not isinstance(result, dict) or result.get("state") not in ("signed_in", "signed_out", "pending", "failed"):
            raise ValueError("Invalid sign-in response.")
        auth = {"state": result["state"]}
        if result.get("verification_url") == "https://auth.openai.com/codex/device" and re.fullmatch(r"[A-Z0-9]{4,6}-[A-Z0-9]{4,6}", result.get("user_code", "")):
            auth.update(verification_url=result["verification_url"], user_code=result["user_code"])
        with self.lock:
            self.auth = auth

    def _run(self, action, request_id, text, replay, uploads=()):
        identifiers = []
        inference_started = False
        try:
            if action in ("chat", "replay"):
                if action == "replay":
                    request_id, text = replay[:2]
                    identifiers = replay[2] if len(replay) > 2 else []
                with self.lock:
                    if self.reply_view is None or self.reply_view["id"] != request_id:
                        supplied = text + ("\nFiles: " + ", ".join(i["filename"] for i in uploads) if uploads else "")
                        self.reply_view = {"id": request_id, "text": supplied, "position": None, "terminal": False, "failed": False, "count": 0}
                    self.active_request = self.reply_view
                if uploads:
                    existing = self.client.lookup_reply("console:local", request_id)
                    if existing is not None:
                        metadata = {}
                        for item in existing["items"]:
                            if item["type"] in ("image", "file"):
                                try:
                                    metadata[item["artifact_id"]] = self.client.artifact_info(
                                        "console:local", item["artifact_id"]
                                    )
                                except AgentRequestError:
                                    metadata[item["artifact_id"]] = {"available": False}
                        result = {
                            "text": "\n".join(i["text"] for i in existing["items"] if i["type"] == "text"),
                            "status": existing["reply_status"],
                            "items": existing["items"],
                            "artifacts": metadata,
                        }
                    else:
                        for index, item in enumerate(uploads):
                            info = {key: item[key] for key in ("filename", "mime", "size", "sha256")}
                            identifier = self.client.upload_input(
                                "console:local", request_id, str(index), info, item["stream"]
                            )["artifact_id"]
                            identifiers.append(identifier)
                        inference_started = True
                        result = self.client.console_chat(text, request_id, identifiers)
                else:
                    inference_started = True
                    result = (
                        self.client.console_chat(text, request_id, identifiers)
                        if identifiers
                        else self.client.console_chat(text, request_id)
                    )
                if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                    raise ValueError("Invalid agent response.")
                status = result.get("status", "uncertain")
                with self.lock:
                    self.details = result.get("details")
                    if status in ("completed", "failed"):
                        items = result.get("items")
                        if status == "completed" and not isinstance(items, list):
                            items = [{"type": "text", "text": result["text"]}]
                        self._show_messages(self.active_request, items, result.get("artifacts", {}))
                        self.active_request["terminal"] = True
                        self.active_request["failed"] = status == "failed"
                    if status == "completed":
                        self.draft, self.uncertain = "", None
                        self.notice = "Reply completed."
                    elif status == "failed":
                        self.notice = "Agent could not finish this request. No automatic resend occurred."
                        self.uncertain = None
                    else:
                        self.notice = "Request outcome uncertain. Check the same request before sending new work."
                        self.uncertain = (request_id, text, identifiers) if identifiers else (request_id, text)
            elif action == "reset":
                self.client.console_reset()
                with self.lock:
                    self.transcript, self.details, self.uncertain, self.draft = [], None, None, ""
                    self.attachments = {}
                    self.reply_view = None
                    self.notice = "New conversation started. Personal profile is preserved."
            else:
                self._auth(self.client.login(restart=action == "retry"))
                with self.lock:
                    self.notice = "Sign-in status updated."
        except Exception as error:
            self.refresh_messages()
            with self.lock:
                code = error.status if isinstance(error, AgentRequestError) else None
                if self.active_request is not None and self.active_request["terminal"]:
                    self.notice = "Agent could not finish this request. No automatic resend occurred." if self.active_request["failed"] else "Reply completed."
                    self.uncertain = None
                elif uploads and not inference_started:
                    self.uncertain = None
                    self.notice = "Files could not be accepted. Select files again; no agent work started. Maximum 10 files and 20 MB combined."
                elif action in ("chat", "replay") and code not in (400, 401, 429, 507):
                    self.uncertain = (request_id, text, identifiers) if identifiers else (request_id, text)
                    self.notice = "Agent unavailable or timed out; outcome uncertain. Check the same request before sending new work."
                else:
                    self.notice = {
                        400: "Message or files were rejected. Select valid PNG/JPEG images or documents again; maximum 10 files and 20 MB combined.",
                        401: "Codex is signed out. Use Sign in.",
                        429: "Agent is busy. No work queued; try again when it finishes.",
                        507: "Workspace storage limit reached. No work started; ask the deployment owner to free temporary or saved files.",
                    }.get(code, "Action could not finish. Check agent status and retry explicitly.")
        finally:
            for item in uploads:
                item["stream"].close()
            with self.lock:
                self.pending = False
                self.active_request = None


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        read_timeout = 10

        def setup(self):
            super().setup()
            self.deadline = threading.Timer(self.read_timeout, self.expire)
            self.deadline.daemon = True
            self.deadline.start()

        def expire(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self):
            self.deadline.cancel()
            try:
                super().finish()
            except OSError:
                pass

        def log_message(self, *_args):
            pass

        def send(self, status, body=b"", cookie=False):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            if cookie:
                self.send_header("Set-Cookie", f"console_session={state.cookie}; HttpOnly; SameSite=Strict; Path=/")
            if status == 303:
                self.send_header("Location", "/")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def trusted(self, post=False):
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or hosts[0] not in ("localhost:8088", "127.0.0.1:8088"):
                return False
            origins = self.headers.get_all("Origin", [])
            return (len(origins) == 1 and origins[0] == "http://" + hosts[0]) if post or origins else True

        def do_GET(self):
            self.deadline.cancel()
            if not self.trusted():
                return self.send(403)
            if self.path == "/health":
                return self.send(200, b"Ready")
            if self.path.startswith("/artifact/"):
                return self.artifact()
            if self.path != "/":
                return self.send(404)
            if self.authenticated():
                state.refresh_messages()
            self.send(200, render(state.snapshot()).encode(), cookie=True)

        def authenticated(self):
            cookies = SimpleCookie()
            values = self.headers.get_all("Cookie", [])
            if len(values) != 1:
                return False
            try:
                cookies.load(values[0])
                value = cookies.get("console_session")
                return value is not None and secrets.compare_digest(value.value, state.cookie)
            except Exception:
                return False

        def artifact(self):
            if not self.authenticated():
                return self.send(403)
            match = re.fullmatch(r"/artifact/([a-f0-9]{32})(/download)?", self.path)
            if match is None:
                return self.send(404)
            with state.lock:
                info = state.attachments.get(match[1])
            if not info:
                return self.send(404, b"Attachment unavailable or expired.")
            started = False
            try:
                with state.client.artifact_stream("console:local", match[1]) as source:
                    self.deadline = threading.Timer(90, self.expire)
                    self.deadline.daemon = True
                    self.deadline.start()
                    inline = not match[2] and info.get("presentation_mime") in ("image/png", "image/jpeg")
                    self.send_response(200)
                    self.send_header("Content-Type", info["presentation_mime"] if inline else "application/octet-stream")
                    self.send_header("Content-Disposition", ("inline" if inline else "attachment") + "; filename=\"attachment\"; filename*=UTF-8''" + quote(info["filename"], safe=""))
                    self.send_header("Content-Length", str(info["size"]))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
                    self.send_header("Connection", "close")
                    self.close_connection = True
                    self.end_headers()
                    started = True
                    remaining = info["size"]
                    while remaining:
                        chunk = source.read(min(65536, remaining))
                        if not chunk:
                            raise OSError("Incomplete attachment stream")
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except Exception:
                if not started:
                    self.send(410, b"Attachment unavailable or expired.")

        def do_POST(self):
            try:
                if not self.trusted(post=True):
                    return self.send(403)
                cookies = SimpleCookie()
                cookie_headers = self.headers.get_all("Cookie", [])
                if len(cookie_headers) != 1:
                    return self.send(403)
                cookies.load(cookie_headers[0])
                cookie = cookies.get("console_session")
                if cookie is None or not secrets.compare_digest(cookie.value, state.cookie):
                    return self.send(403)
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    return self.send(400)
                length = int(self.headers["Content-Length"])
                if self.path == "/chat" and self.headers.get("Content-Type", "").startswith("multipart/form-data"):
                    if not 0 < length <= 20131072 or not _upload_slot.acquire(blocking=False):
                        return self.send(400, b"Upload busy or exceeds 20 MB combined.")
                    files = []
                    transferred = False
                    try:
                        self.deadline.cancel()
                        self.deadline = threading.Timer(90, self.expire)
                        self.deadline.daemon = True
                        self.deadline.start()
                        values, files = parse_upload(
                            self.rfile, length, self.headers["Content-Type"], state.csrf, state.check_upload_nonce
                        )
                        self.deadline.cancel()
                        transferred = state.submit("chat", values["nonce"], values["text"], files)
                        return self.send(303)
                    finally:
                        if not transferred:
                            for item in files:
                                item["stream"].close()
                        _upload_slot.release()
                if not 0 < length <= 200000 or self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
                    return self.send(400)
                values = parse_qs(
                    self.rfile.read(length).decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=4,
                )
                self.deadline.cancel()
                action = self.path.removeprefix("/")
                expected = {"csrf", "nonce", "text"} if action == "chat" else {"csrf", "nonce"}
                if (
                    action not in ("chat", "reset", "status", "login", "retry", "replay")
                    or set(values) != expected
                    or any(len(value) != 1 for value in values.values())
                ):
                    return self.send(400)
                if not secrets.compare_digest(values["csrf"][0], state.csrf):
                    return self.send(403)
                if action == "status":
                    state.refresh_status()
                else:
                    state.submit(action, values["nonce"][0], values.get("text", [""])[0])
                self.send(303)
            except PermissionError:
                self.send(403, b"Invalid upload authentication.")
            except (ValueError, UnicodeError):
                self.send(
                    400,
                    b"Invalid or stale form. Return to the console and select files again. Maximum 10 files and 20 MB combined.",
                )
            except Exception:
                self.send(503, b"Console action unavailable.")

    return Handler


def main():
    os.umask(0o077)
    client = AgentClient(os.environ.get("AGENT_URL", "http://agent:8080"), read_secret("AGENT_API_TOKEN"))
    with AgentHTTPServer(("0.0.0.0", 8088), make_handler(ConsoleState(client))) as server:
        print("Local test console ready.", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
