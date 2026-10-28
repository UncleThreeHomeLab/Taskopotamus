"""Separate private control and public callback listeners; no Google token endpoint."""
import json
import os
import secrets
import socket
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl, urlsplit

from src.agent.http_server import AgentHTTPServer
from src.runtime import read_secret
from .config import GoogleConfig
from .oauth import GoogleOAuth, GoogleUnavailable
from .store import GoogleStore


def make_handler(oauth, control_secret, public=False, configuration_error=False):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.deadline = threading.Timer(10, self.expire)
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

        def send(self, status, payload, headers=()):
            body = json.dumps(payload).encode() if isinstance(payload, dict) else payload.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json" if isinstance(payload, dict) else "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            try:
                if len(self.path) > 8192 or len(self.headers.get_all("Host", [])) != 1:
                    return self.send(400, {"error": "Invalid Google request."})
                if not public and self.path == "/health" and self.command == "GET":
                    return self.send(200, {"ok": True})
                if public:
                    return self.browser()
                if (self.headers.get("Origin") is not None
                        or self.headers["Host"] not in ("google:8081", "localhost:8081", "127.0.0.1:8081")
                        or len(self.headers.get_all("Authorization", [])) != 1
                        or not secrets.compare_digest(self.headers.get("Authorization", "").encode(),
                                                      ("Bearer " + control_secret).encode())):
                    return self.send(403, {"error": "Forbidden"})
                if self.command != "POST" or self.path != "/control":
                    return self.send(404, {"error": "Not found"})
                if (self.headers.get("Transfer-Encoding") is not None
                        or len(self.headers.get_all("Content-Length", [])) != 1
                        or self.headers.get_content_type() != "application/json"):
                    return self.send(400, {"error": "Invalid Google request."})
                length = int(self.headers["Content-Length"])
                if not 0 < length <= 4096:
                    return self.send(413, {"error": "Google request too large."})
                payload = json.loads(self.rfile.read(length))
                if (not isinstance(payload, dict) or not {"chat_id", "action", "request_id"} <= payload.keys()
                        or not payload.keys() <= {"chat_id", "action", "request_id", "confirmation"}):
                    raise ValueError()
                self.deadline.cancel()
                if not oauth.config.enabled:
                    return self.send(200, {"state": "unavailable" if configuration_error else "disabled"})
                return self.send(200, oauth.control(payload["chat_id"], payload["request_id"], payload["action"],
                                                   payload.get("confirmation")))
            except (ValueError, UnicodeError, TypeError):
                self.send(400, {"error": "Google request invalid or expired. Return to Telegram and send /google."})
            except GoogleUnavailable:
                self.send(503, {"error": "Google request unavailable. Return to Telegram and send /google."})
            except Exception:
                self.send(503, {"error": "Google connection unavailable. Return to Telegram and send /google."})

        def browser(self):
            if not oauth.config.enabled or self.command != "GET":
                return self.send(404, "Not found.")
            redirect = urlsplit(oauth.config.redirect_uri)
            if self.headers["Host"] != redirect.netloc:
                return self.send(400, "Invalid Google callback host.")
            parts = urlsplit(self.path)
            pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
            parameters = dict(pairs)
            if len(parameters) != len(pairs) or any(not v or len(v) > 4096 for _, v in pairs):
                raise ValueError()
            if parts.path == "/oauth/google/start" and set(parameters) == {"ticket"}:
                location, cookie = oauth.start(parameters["ticket"])
                flags = "; Path=/oauth/google/; HttpOnly; SameSite=Lax; Max-Age=600"
                if redirect.scheme == "https":
                    flags += "; Secure"
                return self.send(303, "Continue Google login in your browser.",
                                 (("Location", location), ("Set-Cookie", "google_oauth=" + cookie + flags)))
            if parts.path != redirect.path or not set(parameters) <= {"state", "code", "error", "scope", "authuser", "prompt", "hd"}:
                return self.send(404, "Not found.")
            if not parameters.get("state") or ("code" in parameters) == ("error" in parameters):
                raise ValueError()
            if len(self.headers.get_all("Cookie", [])) != 1:
                raise ValueError()
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            cookie = cookies.get("google_oauth")
            if cookie is None:
                raise ValueError()
            self.deadline.cancel()
            oauth.callback(parameters["state"], cookie.value, parameters.get("code"), "error" in parameters)
            return self.send(200, "Return to your private Telegram chat and send /google to review the connection.",
                             (("Set-Cookie", "google_oauth=; Path=/oauth/google/; HttpOnly; SameSite=Lax; Max-Age=0"),))

    return Handler


def main():
    os.umask(0o077)
    error = False
    try:
        config = GoogleConfig.from_env()
    except ValueError:
        config, error = GoogleConfig(), True
        print("Google integration disabled: configuration is incomplete or invalid.", file=sys.stderr)
    secret = read_secret("GOOGLE_CONTROL_SECRET")
    if len(secret) < 32 or not secret.isascii():
        raise ValueError("Invalid Google control secret.")
    store = GoogleStore(os.environ.get("GOOGLE_STORAGE_DIRECTORY", "/var/lib/taskopotamus/google"),
                        read_secret("GOOGLE_TOKEN_ENCRYPTION_KEY"))
    oauth = GoogleOAuth(config, store)
    with AgentHTTPServer(("0.0.0.0", 8081), make_handler(oauth, secret, configuration_error=error)) as control:
        with AgentHTTPServer(("0.0.0.0", 8080), make_handler(oauth, secret, public=True)) as callback:
            thread = threading.Thread(target=callback.serve_forever, daemon=True)
            thread.start()
            try:
                control.serve_forever()
            finally:
                callback.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        print("Google integration failed to start. Check private configuration and storage.", file=sys.stderr)
        sys.exit(1)
