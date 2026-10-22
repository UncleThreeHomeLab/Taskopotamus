import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


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


@contextmanager
def private_database(path):
    path = Path(path)
    private_directory(path.parent)
    with closing(sqlite3.connect(path)) as db, db:
        for suffix in ("", "-journal", "-wal", "-shm"):
            state_file = Path(str(path) + suffix)
            if state_file.exists():
                state_file.chmod(0o600)
        yield db
