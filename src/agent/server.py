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
from .profile_client import ProfileClient
from src.runtime import MAX_JSON_RESPONSE_BYTES, read_secret
from src.profiles.instructions import load_system_instructions
from src.profiles.auth import CONSOLE_CHAT_ID, ProfileTokens
from src.storage.database import Database
from .worker_transport import configure_transport, workspace_status, WorkspaceStorageError
from .subagents import SubagentPolicy
from .runtime_session import RuntimeSessions
from src.messaging.artifacts import ArtifactPolicy, ArtifactStore
from src.messaging.delivery_store import DeliveryStore
from src.messaging.http_api import MessagingAPI


def make_handler(auth, api_token, chat=None, messaging=None, google_data=None):
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
                if google_data is not None and google_data.dispatch(self):
                    return
                if messaging is not None and messaging.dispatch(self, api_token):
                    return
                if not (
                    self.command == "POST" and self.path in ("/chat", "/chat/reset", "/console/chat", "/console/reset")
                ):
                    self.read_deadline.cancel()
                if self.command == "GET" and self.path == "/health":
                    data, status = {"ok": True}, 200
                elif not secrets.compare_digest(
                    self.headers.get("Authorization", "").encode(),
                    f"Bearer {api_token}".encode(),
                ):
                    data, status = {"error": "Unauthorized"}, 403
                elif self.command == "GET" and self.path == "/auth/status":
                    data, status = auth.status(), 200
                elif self.command == "POST" and self.path == "/auth/login":
                    data, status = auth.login(), 200
                elif self.command == "POST" and self.path == "/auth/login/retry":
                    data, status = auth.login(restart=True), 200
                elif (
                    self.command == "POST"
                    and self.path in ("/chat", "/chat/reset", "/console/chat", "/console/reset")
                    and chat is not None
                ):
                    if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                        raise ValueError("Invalid request framing.")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 131072:
                        raise ValueError("Invalid message size.")
                    payload = json.loads(self.rfile.read(length))
                    self.read_deadline.cancel()
                    if not isinstance(payload, dict):
                        raise ValueError("Expected a JSON object.")
                    console = self.path.startswith("/console/")
                    if console:
                        expected = {"text", "request_id"} if self.path == "/console/chat" else set()
                        if set(payload) != expected and not (
                            self.path == "/console/chat" and set(payload) == expected | {"attachments"}
                        ):
                            raise ValueError("Invalid console fields.")
                        chat_id = CONSOLE_CHAT_ID
                    else:
                        chat_id = payload.get("chat_id")
                        ProfileTokens.validate_telegram_chat(chat_id)
                    if self.path in ("/chat/reset", "/console/reset"):
                        data = chat.reset(chat_id)
                    elif console:
                        options = {"attachments": payload["attachments"]} if "attachments" in payload else {}
                        data = chat.reply(
                            chat_id, payload.get("text"), payload.get("request_id"), include_details=True, **options
                        )
                    else:
                        options = {"attachments": payload["attachments"]} if "attachments" in payload else {}
                        data = chat.reply(chat_id, payload.get("text"), payload.get("request_id"), **options)
                    status = 200
                else:
                    data, status = {"error": "Not found"}, 404
            except (ValueError, UnicodeError) as error:
                print(f"Agent request rejected ({type(error).__name__}).", file=sys.stderr, flush=True)
                data, status = {"error": "Invalid request"}, 400
            except PermissionError:
                data, status = {"error": "Codex sign-in required"}, 401
            except BlockingIOError:
                data, status = {"error": "Agent busy"}, 429
            except WorkspaceStorageError:
                data, status = {"error": "Workspace storage limit reached"}, 507
            except TimeoutError:
                data, status = {"error": "Request timed out"}, 504
            except Exception as error:
                print(f"Agent request failed ({type(error).__name__}).", file=sys.stderr, flush=True)
                data, status = {"error": "Agent request failed"}, 503
            self.send_json(data, status)

        def send_json(self, data, status):
            try:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                if len(body) > MAX_JSON_RESPONSE_BYTES:
                    raise ValueError("Response exceeds supported envelope.")
            except (ValueError, UnicodeError):
                body, status = b'{"error":"Agent response unavailable"}', 503
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
    load_system_instructions()
    if os.environ.get("CODEX_BIN") != "/usr/local/bin/codex-worker":
        raise ValueError("Full-access execution requires the isolated worker transport.")
    api_token = read_secret("AGENT_API_TOKEN")
    compact_percent = os.environ.get("CODEX_AUTO_COMPACT_PERCENT", "").strip()
    profiles = ProfileClient(os.environ.get("PROFILE_SERVICE_URL", "http://mcp:8080"), read_secret("PROFILE_SIGNING_KEY"), worker_scopes=True)
    subagents = SubagentPolicy(
        os.environ.get("CODEX_SUBAGENT_MODEL", "gpt-6-luna"),
        os.environ.get("CODEX_SUBAGENT_REASONING_EFFORT", "high"),
        int(os.environ.get("CODEX_SUBAGENT_MAX_THREADS", "2")),
    )
    timeout = int(os.environ.get("CODEX_TURN_TIMEOUT_SECONDS", "600"))
    if not 30 <= timeout <= 1800:
        raise ValueError("Turn timeout must be between 30 and 1800 seconds.")
    codex_config = CodexConfig(codex_bin=os.environ["CODEX_BIN"], config_overrides=('cli_auth_credentials_store="file"',))
    with configure_transport():
        def runtime_factory():
            try:
                return Codex(CodexConfig(
                    codex_bin=codex_config.codex_bin,
                    config_overrides=codex_config.config_overrides,
                    env={"TASKOPOTAMUS_WORKER_MODE": "warm"},
                ))
            except Exception:
                if workspace_status()["admission_blocked"]:
                    raise WorkspaceStorageError("Workspace storage limit reached.") from None
                raise
        sessions = RuntimeSessions(runtime_factory, profile_url=profiles.url,
                                   agent_ip=os.environ.get("WORKER_AGENT_IP", "10.203.17.30"),
                                   cleanup_status=workspace_status)
        auth = CodexAuth(lambda: Codex(CodexConfig(
            codex_bin=codex_config.codex_bin, config_overrides=codex_config.config_overrides,
            env={"TASKOPOTAMUS_WORKER_MODE": "auth"},
        )), operation_scope=sessions.authentication)
        database = Database.from_env()
        policy = ArtifactPolicy.from_env()
        deliveries = DeliveryStore(database, retention=policy.retention_seconds)
        artifacts = ArtifactStore(database, os.environ.get("ARTIFACT_DIRECTORY", "/var/lib/taskopotamus/artifacts"), policy)
        chat = ChatService(
            runtime_factory,
            database,
            model=os.environ.get("CODEX_MODEL", "gpt-6.1-sol"),
            effort=os.environ.get("CODEX_REASONING_EFFORT", "medium"),
            auto_compact_percent=int(compact_percent) if compact_percent else None,
            profiles=profiles,
            subagents=subagents, turn_timeout=timeout, workspace_status=workspace_status,
            artifacts=artifacts, deliveries=deliveries, runtime_sessions=sessions,
        )
        stopped = threading.Event()
        def maintain():
            while not stopped.is_set():
                try:
                    with database.connect() as db:
                        deliveries.maintain(db)
                    artifacts.maintain()
                except Exception:
                    print("Messaging maintenance failed; retry scheduled.", file=sys.stderr, flush=True)
                stopped.wait(policy.sweep_seconds)
        maintenance = threading.Thread(target=maintain, daemon=True)
        maintenance.start()
        from .google_data import GoogleDataAPI
        google_secret = read_secret("GOOGLE_DATA_SECRET")
        if len(google_secret) < 32 or not google_secret.isascii():
            raise ValueError("Invalid Google data secret.")
        google_data = GoogleDataAPI(artifacts, google_secret)
        with AgentHTTPServer(("0.0.0.0", 8080), make_handler(auth, api_token, chat, MessagingAPI(deliveries, artifacts), google_data)) as server:
            print("Agent service ready.", flush=True)
            try:
                server.serve_forever()
            finally:
                try:
                    sessions.shutdown()
                finally:
                    stopped.set()
                    maintenance.join(timeout=10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"Agent service failed to start ({type(error).__name__}). Check Codex runtime and storage permissions.", file=sys.stderr)
        sys.exit(1)
