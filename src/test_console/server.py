"""Opt-in localhost browser interface; live work starts only from protected forms."""
import os
import re
import secrets
import socket
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs

from src.agent.http_server import AgentHTTPServer
from src.runtime import read_secret
from src.telegram.agent_client import AgentClient, AgentRequestError
from .page import render


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

    def snapshot(self):
        with self.lock:
            return {key: getattr(self, key) for key in
                    ("csrf", "nonce", "pending", "transcript", "auth", "notice", "draft", "details", "uncertain")}

    def submit(self, action, nonce, text=""):
        with self.lock:
            if nonce in self.jobs:
                previous = self.jobs[nonce]
                if previous != (action, text):
                    raise ValueError("Submission changed.")
                return
            if nonce != self.nonce or self.pending:
                raise ValueError("Stale submission. Refresh the page.")
            if action == "chat" and (not text.strip() or len(text) > 16000):
                raise ValueError("Message must contain 1 to 16000 characters.")
            if action == "replay" and self.uncertain is None:
                raise ValueError("No uncertain request.")
            replay = self.uncertain
            self.jobs[nonce] = (action, text)
            while len(self.jobs) > 64:
                del self.jobs[next(iter(self.jobs))]
            self.nonce = str(secrets.randbelow(10**20))
            self.pending = True
            self.notice = "Processing. This page refreshes while work is running."
            self.draft = text if action == "chat" else self.draft
        threading.Thread(target=self._run, args=(action, nonce, text, replay), daemon=True).start()

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

    def _run(self, action, request_id, text, replay):
        try:
            if action in ("chat", "replay"):
                if action == "replay":
                    request_id, text = replay
                result = self.client.console_chat(text, request_id)
                if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                    raise ValueError("Invalid agent response.")
                status = result.get("status", "uncertain")
                with self.lock:
                    self.details = result.get("details")
                    if status == "completed":
                        self.transcript = (self.transcript + [("You", text), ("Agent", result["text"][:32000])])[-20:]
                        self.draft, self.uncertain = "", None
                        self.notice = "Reply completed."
                    elif status == "failed":
                        self.notice = "Agent could not finish this request. No automatic resend occurred."
                        self.uncertain = None
                    else:
                        self.notice = "Request outcome uncertain. Check the same request before sending new work."
                        self.uncertain = (request_id, text)
            elif action == "reset":
                self.client.console_reset()
                with self.lock:
                    self.transcript, self.details, self.uncertain, self.draft = [], None, None, ""
                    self.notice = "New conversation started. Personal profile is preserved."
            else:
                self._auth(self.client.login(restart=action == "retry"))
                with self.lock:
                    self.notice = "Sign-in status updated."
        except Exception as error:
            with self.lock:
                code = error.status if isinstance(error, AgentRequestError) else None
                if action in ("chat", "replay") and code not in (400, 401, 429, 507):
                    self.uncertain = (request_id, text)
                    self.notice = "Agent unavailable or timed out; outcome uncertain. Check the same request before sending new work."
                else:
                    self.notice = {401: "Codex is signed out. Use Sign in.", 429: "Agent is busy. No work queued; try again when it finishes.",
                                   507: "Workspace storage limit reached. No work started; ask the deployment owner to free temporary or saved files."}.get(
                        code, "Action could not finish. Check agent status and retry explicitly.")
        finally:
            with self.lock:
                self.pending = False


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
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
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
            if self.path != "/":
                return self.send(404)
            self.send(200, render(state.snapshot()).encode(), cookie=True)

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
                if not 0 < length <= 65536 or self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
                    return self.send(400)
                values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=4)
                self.deadline.cancel()
                action = self.path.removeprefix("/")
                expected = {"csrf", "nonce", "text"} if action == "chat" else {"csrf", "nonce"}
                if action not in ("chat", "reset", "status", "login", "retry", "replay") or set(values) != expected or any(len(value) != 1 for value in values.values()):
                    return self.send(400)
                if not secrets.compare_digest(values["csrf"][0], state.csrf):
                    return self.send(403)
                if action == "status":
                    state.refresh_status()
                else:
                    state.submit(action, values["nonce"][0], values.get("text", [""])[0])
                self.send(303)
            except (ValueError, UnicodeError):
                self.send(400, b"Invalid or stale form. Refresh the page and try again.")
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
