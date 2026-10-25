import json
import sys
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener
from urllib.parse import quote

from src.runtime import MAX_JSON_RESPONSE_BYTES


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class AgentRequestError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AgentClient:
    def __init__(self, base_url, api_token):
        self.base_url = base_url.rstrip("/")
        if len(api_token) < 32 or not api_token.isascii():
            raise ValueError("Agent API token must contain at least 32 ASCII characters.")
        self.api_token = api_token
        self.chat_timeout = int(os.environ.get("CODEX_TURN_TIMEOUT_SECONDS", "600")) + 20
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def request(self, path, method="GET", body=None):
        request = Request(
            f"{self.base_url}{path}", method=method,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_token}"},
        )
        try:
            with self.opener.open(request, timeout=self.chat_timeout if path in ("/chat", "/console/chat") else 60) as response:
                body = response.read(MAX_JSON_RESPONSE_BYTES + 1)
            if len(body) > MAX_JSON_RESPONSE_BYTES:
                raise ValueError("Agent response too large.")
            return json.loads(body)
        except HTTPError as error:
            print(f"Agent HTTP request failed ({error.code}).", file=sys.stderr, flush=True)
            error.close()
            if error.code == 400:
                raise AgentRequestError(
                    "Message or files were rejected. Check valid PNG/JPEG images, file names and the 20 MB combined limit, then send again.",
                    400,
                ) from None
            if error.code == 401:
                raise AgentRequestError("Codex is signed out. Send /codex_login.", 401) from None
            if error.code == 429:
                raise AgentRequestError("Agent is busy. Please try again shortly.", 429) from None
            if error.code == 504:
                raise AgentRequestError("Codex reply timed out. Please send your message again.", 504) from None
            if error.code == 507:
                raise AgentRequestError("Workspace storage limit reached. Ask the deployment owner to free temporary or saved files.", 507) from None
            raise AgentRequestError("Agent service unavailable. Check Codex account access and try again.", error.code) from None
        except (URLError, OSError, ValueError) as error:
            print(f"Agent connection failed ({type(error).__name__}).", file=sys.stderr, flush=True)
            raise AgentRequestError("Agent service unavailable. Try again shortly.") from None

    def login(self, restart=False):
        path = "/auth/login/retry" if restart else "/auth/login"
        return self.request(path, "POST")

    def status(self):
        return self.request("/auth/status")

    def chat(self, chat_id, text, request_id, attachments=None):
        body = {"chat_id": str(chat_id), "text": text, "request_id": str(request_id)}
        if attachments:
            body["attachments"] = attachments
        return self.request("/chat", "POST", body)

    def reset(self, chat_id):
        return self.request("/chat/reset", "POST", {"chat_id": str(chat_id)})

    def console_chat(self, text, request_id, attachments=None):
        body = {"text": text, "request_id": request_id}
        if attachments:
            body["attachments"] = attachments
        return self.request("/console/chat", "POST", body)

    def input_lookup(self, chat_id, request_id, operation):
        return self.request(
            "/messaging/input-lookup",
            "POST",
            {"chat_id": str(chat_id), "request_id": str(request_id), "operation": operation},
        )["artifact"]

    def upload_input(self, chat_id, request_id, operation, info, source):
        previous = self.input_lookup(chat_id, request_id, operation)
        if previous is not None:
            if any(previous.get(key) != info[key] for key in ("filename", "mime", "size", "sha256")):
                raise AgentRequestError("Input upload identity changed.", 400)
            return previous
        meta = {**info, "chat_id": str(chat_id), "request_id": str(request_id), "operation": operation}
        request = Request(
            self.base_url + "/messaging/upload",
            data=source,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.api_token,
                "Content-Type": "application/octet-stream",
                "X-Artifact-Metadata": json.dumps(meta),
                "Content-Length": str(info["size"]),
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(65537)
            if len(body) > 65536:
                raise ValueError()
            return json.loads(body)
        except HTTPError as error:
            code = error.code
            error.close()
            raise AgentRequestError("Input could not be accepted. Maximum 10 files and 20 MB combined.", code) from None
        except (OSError, ValueError):
            raise AgentRequestError("Input upload could not be confirmed.") from None

    def console_reset(self):
        return self.request("/console/reset", "POST", {})

    def publish_reply(self, chat_id, request_id, text, command_attempt=None):
        body = {"chat_id": str(chat_id), "request_id": str(request_id), "text": text}
        if command_attempt is not None:
            body["command_attempt"] = command_attempt
        return self.request("/messaging/publish", "POST", body)

    def lookup_reply(self, chat_id, request_id):
        return self.request("/messaging/lookup", "POST", {"chat_id": str(chat_id), "request_id": str(request_id)})[
            "reply"
        ]

    def reserve_command(self, chat_id, request_id, command):
        return self.request("/messaging/local-command", "POST", {"chat_id": str(chat_id), "request_id": str(request_id), "command": command})

    def claim_delivery(self, allowed_chats):
        return self.request("/messaging/claim", "POST", {"allowed_chats": sorted(allowed_chats)})["delivery"]

    def acknowledge_delivery(self, delivery, state, receipt=None):
        return self.request("/messaging/receipt", "POST", {
            "delivery_id": delivery["delivery_id"], "sequence": delivery["sequence"], "attempt": delivery["attempt"],
            "state": state, "receipt": receipt,
        })

    def delivery_status(self, chat_id):
        return self.request("/messaging/status", "POST", {"chat_id": str(chat_id)})["items"]

    def import_legacy_delivery(self, chat_id, request_id, chunks, next_chunk):
        return self.request("/messaging/import-legacy", "POST", {
            "chat_id": str(chat_id), "request_id": str(request_id), "chunks": chunks, "next_chunk": next_chunk,
        })

    def artifact_info(self, chat_id, artifact_id):
        return self.request("/messaging/metadata/" + quote(artifact_id, safe="") + "?chat_id=" + quote(str(chat_id), safe=""))

    def artifact_stream(self, chat_id, artifact_id):
        request = Request(self.base_url + "/messaging/artifact/" + quote(artifact_id, safe="") + "?chat_id=" + quote(str(chat_id), safe=""),
                          headers={"Authorization": "Bearer " + self.api_token})
        try:
            return self.opener.open(request, timeout=10)
        except (HTTPError, URLError, OSError) as error:
            if isinstance(error, HTTPError):
                error.close()
            raise AgentRequestError("Attachment unavailable or expired.") from None
