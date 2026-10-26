import os
from pathlib import Path


# Includes duplicated legacy text, ten captions, Unicode escaping, and bounded run telemetry.
MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024


def read_secret(name, env=None):
    env = os.environ if env is None else env
    try:
        value = Path(env[f"{name}_FILE"]).read_text().strip() if env.get(f"{name}_FILE") else env.get(name, "").strip()
    except OSError:
        raise ValueError(f"Cannot read {name} secret file.") from None
    if not value:
        raise ValueError(f"Set {name} or {name}_FILE.")
    return value


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path
