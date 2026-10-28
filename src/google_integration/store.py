"""Private encrypted OAuth vault and atomic lifecycle transitions."""
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.runtime import private_directory


def digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def validate_chat(chat):
    if not isinstance(chat, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", chat):
        raise ValueError("Invalid private chat.")


class GoogleStore:
    def __init__(self, directory, key, clock=time.time):
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Google encryption key must contain 64 lowercase hexadecimal characters.")
        root = Path(directory)
        if root.is_symlink() or (root / "oauth.sqlite3").is_symlink():
            raise ValueError("Google storage cannot use symbolic links.")
        self.path = private_directory(root) / "oauth.sqlite3"
        self.cipher, self.clock = AESGCM(bytes.fromhex(key)), clock
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS connections (
                    chat TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'disconnected', active BLOB,
                    pending BLOB, pending_hash TEXT, pending_until REAL,
                    last_update INTEGER NOT NULL DEFAULT -1);
                CREATE TABLE IF NOT EXISTS flows (
                    chat TEXT PRIMARY KEY, generation INTEGER NOT NULL, ticket_hash TEXT UNIQUE,
                    state_hash TEXT UNIQUE, cookie_hash TEXT, payload BLOB NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS commands (
                    chat TEXT NOT NULL, request_id TEXT NOT NULL, action_hash TEXT NOT NULL,
                    generation INTEGER NOT NULL, response BLOB NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(chat,request_id));
                CREATE TABLE IF NOT EXISTS permissions (
                    chat TEXT PRIMARY KEY, generation INTEGER NOT NULL, revision INTEGER NOT NULL, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS menus (
                    chat TEXT PRIMARY KEY, generation INTEGER NOT NULL, revision INTEGER NOT NULL,
                    expires REAL NOT NULL, payload BLOB NOT NULL);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self, deadline=None):
        timeout = 5 if deadline is None else max(0, min(5, deadline - time.monotonic()))
        db = sqlite3.connect(self.path, timeout=timeout)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Google storage timed out.")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def seal(self, chat, generation, purpose, value):
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, json.dumps(value).encode(),
                                           f"{chat}:{generation}:{purpose}".encode())

    def open(self, chat, generation, purpose, value):
        return json.loads(self.cipher.decrypt(value[:12], value[12:],
                                              f"{chat}:{generation}:{purpose}".encode()))

    def connection(self, db, chat):
        validate_chat(chat)
        db.execute("INSERT OR IGNORE INTO connections(chat) VALUES (?)", (chat,))
        return db.execute("SELECT * FROM connections WHERE chat=?", (chat,)).fetchone()

    def cleanup(self, db):
        now = self.clock()
        db.execute("DELETE FROM flows WHERE expires<=?", (now,))
        db.execute("DELETE FROM commands WHERE expires<=?", (now,))
        db.execute("DELETE FROM menus WHERE expires<=?", (now,))
        db.execute("UPDATE connections SET pending=NULL,pending_hash=NULL,pending_until=NULL WHERE pending_until<=?", (now,))

    def cached(self, db, chat, request_id, action):
        if (not isinstance(request_id, str) or not re.fullmatch(r"[0-9]{1,19}", request_id)
                or int(request_id) > 9223372036854775807):
            raise ValueError("Invalid Google command identity.")
        self.cleanup(db)
        row = self.connection(db, chat)
        saved = db.execute("SELECT * FROM commands WHERE chat=? AND request_id=?", (chat, request_id)).fetchone()
        if saved:
            if saved["action_hash"] != digest(action):
                raise ValueError("Google command identity changed.")
            return self.open(chat, saved["generation"], "command:" + request_id, saved["response"])
        if int(request_id) <= row["last_update"]:
            raise ValueError("Google command expired. Send a new command.")
        return None

    def save_command(self, db, chat, request_id, action, response):
        row = self.connection(db, chat)
        db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?)", (
            chat, request_id, digest(action), row["generation"],
            self.seal(chat, row["generation"], "command:" + request_id, response), self.clock() + 86400))
        db.execute("UPDATE connections SET last_update=? WHERE chat=?", (int(request_id), chat))

    def status(self, chat):
        with self.transaction() as db:
            self.cleanup(db)
            row = self.connection(db, chat)
            result = {"state": row["status"], "gmail": "off", "calendars": "off"}
            if row["active"]:
                result["email"] = self.open(chat, row["generation"], "active", row["active"])["email"]
            if row["pending"]:
                pending = self.open(chat, row["generation"], "pending", row["pending"])
                result["pending"] = {"email": pending["email"], "confirmation": pending["confirmation"]}
            return result
