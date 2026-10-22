import json
import os
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler

from openai_codex import Codex, CodexConfig

from .codex_auth import CodexAuth
from .chat_service import ChatService
from .http_server import AgentHTTPServer
from src.runtime import private_directory, read_secret


def make_handler(auth, api_token, chat=None):
    if len(api_token) < 32 or not api_token.isascii():
        raise ValueError("Agent API token must contain at least 32 ASCII characters.")

    class Handler(BaseHTTPRequestHandler):
        read_timeout = 10

        def setup(self):
            super().setup()
            # An absolute deadline also stops clients that drip bytes below the socket timeout.
            self.read_deadline = threading.Timer(self.read_timeout, self.expire_request)
            self.read_deadline.daemon = True
            self.read_deadline.start()

        def expire_request(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self):
            self.read_deadline.cancel()
            try:
                super().finish()
            except OSError:
                pass

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            try:
                if not (self.command == "POST" and self.path in ("/chat", "/chat/reset")):
                    self.read_deadline.cancel()
                if self.command == "GET" and self.path == "/health":
                    data, status = {"ok": True}, 200
                elif not secrets.compare_digest(
                    self.headers.get("Authorization", "").encode(), f"Bearer {api_token}".encode(),
                ):
                    data, status = {"error": "Unauthorized"}, 403
                elif self.command == "GET" and self.path == "/auth/status":
                    data, status = auth.status(), 200
                elif self.command == "POST" and self.path == "/auth/login":
                    data, status = auth.login(), 200
                elif self.command == "POST" and self.path == "/auth/login/retry":
                    data, status = auth.login(restart=True), 200
                elif self.command == "POST" and self.path in ("/chat", "/chat/reset") and chat is not None:
                    if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                        raise ValueError("Invalid request framing.")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 32768:
                        raise ValueError("Invalid message size.")
                    payload = json.loads(self.rfile.read(length))
                    self.read_deadline.cancel()
                    if not isinstance(payload, dict):
                        raise ValueError("Expected a JSON object.")
                    chat_id = payload.get("chat_id")
                    if self.path == "/chat/reset":
                        data = chat.reset(chat_id)
                    else:
                        data = chat.reply(chat_id, payload.get("text"), payload.get("request_id"))
                    status = 200
                else:
                    data, status = {"error": "Not found"}, 404
            except (ValueError, UnicodeError):
                data, status = {"error": "Invalid request"}, 400
            except PermissionError:
                data, status = {"error": "Codex sign-in required"}, 401
            except BlockingIOError:
                data, status = {"error": "Agent busy"}, 429
            except TimeoutError:
                data, status = {"error": "Request timed out"}, 504
            except Exception:
                data, status = {"error": "Agent request failed"}, 503
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

    return Handler


def main():
    os.umask(0o077)
    state = private_directory(os.environ.get("CODEX_HOME", "/home/agent/.codex"))
    api_token = read_secret("AGENT_API_TOKEN")
    compact_percent = os.environ.get("CODEX_AUTO_COMPACT_PERCENT", "").strip()
    codex_config = CodexConfig(config_overrides=('cli_auth_credentials_store="file"',))
    with Codex(codex_config) as codex:
        auth = CodexAuth(codex)
        chat = ChatService(
            lambda: Codex(codex_config),
            state / "taskopotamus.sqlite3",
            model=os.environ.get("CODEX_MODEL", "gpt-6-astra"),
            effort=os.environ.get("CODEX_REASONING_EFFORT", "medium"),
            auto_compact_percent=int(compact_percent) if compact_percent else None,
        )
        with AgentHTTPServer(("0.0.0.0", 8080), make_handler(auth, api_token, chat)) as server:
            print("Agent service ready.", flush=True)
            server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        print("Agent service failed to start. Check Codex runtime and storage permissions.", file=sys.stderr)
        sys.exit(1)
