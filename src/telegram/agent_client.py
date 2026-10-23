import json
import sys
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AgentClient:
    def __init__(self, base_url, api_token):
        self.base_url = base_url.rstrip("/")
        if len(api_token) < 32 or not api_token.isascii():
            raise ValueError("Agent API token must contain at least 32 ASCII characters.")
        self.api_token = api_token
        self.chat_timeout = int(os.environ.get("CODEX_TURN_TIMEOUT_SECONDS", "600")) + 20

    def request(self, path, method="GET", body=None):
        request = Request(
            f"{self.base_url}{path}", method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_token}"},
        )
        try:
            with urlopen(request, timeout=self.chat_timeout if path == "/chat" else 60) as response:
                return json.load(response)
        except HTTPError as error:
            print(f"Agent HTTP request failed ({error.code}).", file=sys.stderr, flush=True)
            error.close()
            if error.code == 401:
                raise RuntimeError("Codex is signed out. Send /codex_login.") from None
            if error.code == 429:
                raise RuntimeError("Agent is busy. Please try again shortly.") from None
            if error.code == 504:
                raise RuntimeError("Codex reply timed out. Please send your message again.") from None
            raise RuntimeError("Agent service unavailable. Check Codex account access and try again.") from None
        except (URLError, OSError, ValueError) as error:
            print(f"Agent connection failed ({type(error).__name__}).", file=sys.stderr, flush=True)
            raise RuntimeError("Agent service unavailable. Try again shortly.") from None

    def login(self, restart=False):
        path = "/auth/login/retry" if restart else "/auth/login"
        return self.request(path, "POST")

    def status(self):
        return self.request("/auth/status")

    def chat(self, chat_id, text, request_id):
        return self.request("/chat", "POST", {"chat_id": str(chat_id), "text": text, "request_id": str(request_id)})

    def reset(self, chat_id):
        return self.request("/chat/reset", "POST", {"chat_id": str(chat_id)})
