import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from src.profiles.auth import ProfileTokens
from src.profiles.instructions import resolve_profile


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class ProfileClient:
    def __init__(self, url, secret):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Invalid profile service URL.")
        self.url = url.rstrip("/")
        self.tokens = ProfileTokens(secret)
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def prepare(self, chat_id):
        token = self.tokens.issue(chat_id)
        request = Request(self.url + "/profile", headers={"Authorization": f"Bearer {token}"})
        try:
            with self.opener.open(request, timeout=10) as response:
                body = response.read(65537)
            if len(body) > 65536:
                raise ValueError()
            profile = json.loads(body)
            if not isinstance(profile, dict) or not all(key in profile for key in ("identity", "persona", "behavior")):
                raise ValueError()
            profile = resolve_profile(profile)
        except HTTPError as error:
            print(f"Profile read failed (HTTP {error.code}).", file=sys.stderr, flush=True)
            error.close()
            raise RuntimeError("Profile service unavailable.") from None
        except (URLError, OSError, ValueError, KeyError, TypeError) as error:
            print(f"Profile read failed ({type(error).__name__}).", file=sys.stderr, flush=True)
            raise RuntimeError("Profile service unavailable.") from None
        config = {
            "mcp_servers.profiles.url": self.url + "/mcp",
            "mcp_servers.profiles.env_http_headers": {"Authorization": "TASKOPOTAMUS_PROFILE_AUTH"},
            "mcp_servers.profiles.enabled_tools": ["get_agent_profile", "update_agent_profile"],
            "mcp_servers.profiles.required": True,
            "mcp_servers.profiles.tool_timeout_sec": 10,
            "mcp_servers.profiles.tools.get_agent_profile.approval_mode": "auto",
            "mcp_servers.profiles.tools.update_agent_profile.approval_mode": "auto",
        }
        return profile, config, token
