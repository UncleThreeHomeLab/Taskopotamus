"""Owner-supplied Google configuration; never forwarded to the worker."""
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from src.runtime import read_secret


@dataclass(frozen=True)
class GoogleConfig:
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    redirect_uri: str = ""

    @property
    def enabled(self):
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    @property
    def origin(self):
        parts = urlsplit(self.redirect_uri)
        return f"{parts.scheme}://{parts.netloc}"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        values = []
        for name in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI"):
            values.append(read_secret(name, env) if env.get(name + "_FILE") or env.get(name, "").strip() else "")
        config = cls(*values)
        if any(values) and not config.enabled:
            raise ValueError("Google OAuth configuration is incomplete.")
        if config.enabled:
            parts = urlsplit(config.redirect_uri)
            if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                    or parts.query or parts.fragment or parts.path != "/oauth/google/callback"
                    or parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1")):
                raise ValueError("Google OAuth redirect must be HTTPS or localhost and use /oauth/google/callback.")
            if any(ord(c) < 33 or ord(c) > 126 for value in values for c in value):
                raise ValueError("Invalid Google OAuth configuration.")
        return config
