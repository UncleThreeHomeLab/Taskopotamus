"""Read one immutable request credential snapshot at MCP process startup."""
import ipaddress
import json
import math
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit


SCOPE_PATH = Path("/var/lib/taskopotamus/runtime-scope/current.json")


def validate_scope(value, now=None):
    now = time.time() if now is None else now
    if not isinstance(value, dict) or set(value) != {"profile_token", "artifact_token", "agent_ip", "profile_url", "deadline"}:
        raise ValueError("Invalid request scope.")
    if not isinstance(value["profile_token"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,2048}", value["profile_token"]):
        raise ValueError("Invalid request scope.")
    if not isinstance(value["artifact_token"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value["artifact_token"]):
        raise ValueError("Invalid request scope.")
    deadline = value["deadline"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or not now < deadline <= now + 1860:
        raise ValueError("Invalid request deadline.")
    address = ipaddress.IPv4Address(value["agent_ip"])
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise ValueError("Invalid request endpoint.")
    parts = urlsplit(value["profile_url"])
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ("", "/") or len(value["profile_url"]) > 2048):
        raise ValueError("Invalid profile endpoint.")
    return dict(value)


def load_scope(path=SCOPE_PATH, runtime_gid=10001, now=None):
    path = Path(path)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(parent)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError("Unsafe request scope directory.")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != runtime_gid
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o440 or info.st_size > 8192):
            raise PermissionError("Unsafe request scope file.")
        content = source.read(8193)
    if len(content) > 8192:
        raise ValueError("Invalid request scope.")
    return validate_scope(json.loads(content), now)


def credentials():
    if os.environ.get("TASKOPOTAMUS_REQUEST_SCOPE"):
        # Only the deployment's fixed path is accepted; models cannot choose a credential source.
        if os.environ["TASKOPOTAMUS_REQUEST_SCOPE"] != str(SCOPE_PATH):
            raise ValueError("Invalid request scope path.")
        return load_scope()
    return {"artifact_token": os.environ.get("TASKOPOTAMUS_ARTIFACT_AUTH", ""),
            "agent_ip": os.environ.get("TASKOPOTAMUS_AGENT_IP", "")}
