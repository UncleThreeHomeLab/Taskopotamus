"""Private immutable file snapshots and revocable request-scoped worker access."""
import hashlib
import json
import os
import re
import secrets
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .messages import identity, opaque_id, validate_items


class ArtifactCapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactPolicy:
    max_files: int = 10
    input_max_bytes: int = 20000000
    output_max_bytes: int = 50000000
    image_max_bytes: int = 10000000
    capacity_bytes: int = 1073741824
    staging_seconds: int = 3600
    retention_seconds: int = 604800
    sweep_seconds: int = 60

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in self.__dict__.values()):
            raise ValueError("Artifact policy values must be positive integers.")
        if self.max_files > 10 or self.input_max_bytes > 20000000 or self.output_max_bytes > 50000000 or self.image_max_bytes > 10000000:
            raise ValueError("Artifact policy exceeds supported transport limits.")
        if self.staging_seconds > self.retention_seconds:
            raise ValueError("Staging lifetime exceeds artifact lifetime.")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(**{name: int(env.get("ARTIFACT_" + name.upper(), field.default))
                      for name, field in cls.__dataclass_fields__.items()})


def operation_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("Invalid operation identifier.")
    return value


def metadata(filename, mime, size, digest):
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 200 or filename in (".", "..")
            or any(ord(c) < 32 or ord(c) == 127 or c in '/\\' for c in filename)):
        raise ValueError("Invalid display filename.")
    if not isinstance(mime, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.+_-]*/[a-z0-9][a-z0-9.+_-]*", mime) or len(mime) > 100:
        raise ValueError("Invalid media type.")
    if type(size) is not int or size <= 0 or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid file size or digest.")


class ArtifactStore:
    def __init__(self, database, root, policy=None, clock=time.time):
        self.database, self.root, self.policy, self.clock = database, Path(root), policy or ArtifactPolicy(), clock
        if self.root.is_symlink():
            raise ValueError("Artifact storage must be a private directory.")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        with database.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS artifact_runs (
                chat_id TEXT, request_id TEXT, state TEXT NOT NULL, capability TEXT,
                deadline DOUBLE PRECISION NOT NULL, PRIMARY KEY(chat_id,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, request_id TEXT NOT NULL, direction TEXT NOT NULL,
                operation TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL, size BIGINT NOT NULL,
                digest TEXT NOT NULL, state TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,
                expires DOUBLE PRECISION NOT NULL, UNIQUE(chat_id,request_id,direction,operation))""")
            db.execute("""CREATE TABLE IF NOT EXISTS attachment_batches (
                chat_id TEXT, request_id TEXT, operation TEXT, items TEXT NOT NULL,
                PRIMARY KEY(chat_id,request_id,operation))""")
            db.execute("ALTER TABLE attachment_batches ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")
            db.execute("ALTER TABLE attachment_batches ADD COLUMN IF NOT EXISTS request_key TEXT")
            db.execute("ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS presentation_mime TEXT")
            db.execute(
                "CREATE TABLE IF NOT EXISTS accepted_inputs (chat_id TEXT, request_id TEXT, items TEXT NOT NULL, PRIMARY KEY(chat_id,request_id))"
            )

    def input_identity(self, chat_id, request_id, identifiers, available=False, db=None):
        identity(chat_id, request_id)
        if (
            not isinstance(identifiers, list)
            or len(identifiers) > self.policy.max_files
            or any(not isinstance(item, str) for item in identifiers)
            or len(set(identifiers)) != len(identifiers)
        ):
            raise ValueError("Supply up to ten distinct attachments.")
        if db is None:
            with self.database.connect() as connection:
                return self.input_identity(chat_id, request_id, identifiers, available, connection)
        result = []
        for identifier in identifiers:
            opaque_id(identifier)
            row = db.execute(
                "SELECT filename,mime,size,digest,state,expires FROM artifacts WHERE id=%s AND chat_id=%s AND request_id=%s AND direction='input'",
                (identifier, chat_id, request_id),
            ).fetchone()
            if not row or available and (row[4] not in ("prepared", "committed") or row[5] <= self.clock()):
                raise ValueError("Input attachment unavailable or expired.")
            info = {"artifact_id": identifier, "filename": row[0], "mime": row[1], "size": row[2], "sha256": row[3]}
            if available:
                with (self.root / identifier).open("rb") as file:
                    magic = file.read(8)
                    detected = (
                        "image/png"
                        if magic == b"\x89PNG\r\n\x1a\n"
                        else "image/jpeg"
                        if magic.startswith(b"\xff\xd8\xff")
                        else None
                    )
                    if detected or row[1] in ("image/png", "image/jpeg"):
                        from .image_validation import validate_image

                        info["presentation_mime"] = validate_image(file, detected or row[1])
            result.append(info)
        if sum(item["size"] for item in result) > self.policy.input_max_bytes:
            raise ValueError("Attachments exceed the combined 20 MB limit.")
        return result

    def accept_inputs(self, chat_id, request_id, identifiers, seconds):
        from src.worker.input_files import request_tag

        with self.database.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended('artifact-capacity',0))")
            items = self.input_identity(chat_id, request_id, identifiers, True, db)
            serialized = json.dumps(items, sort_keys=True)
            previous = db.execute(
                "SELECT items FROM accepted_inputs WHERE chat_id=%s AND request_id=%s FOR UPDATE", (chat_id, request_id)
            ).fetchone()
            if previous and previous[0] != serialized:
                raise ValueError("Accepted inputs cannot change.")
            db.execute(
                "INSERT INTO accepted_inputs VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (chat_id, request_id, serialized),
            )
            db.execute(
                "UPDATE artifacts SET expires=GREATEST(expires,%s) WHERE id=ANY(%s)",
                (self.clock() + seconds, identifiers),
            )
        return {"request_tag": request_tag(chat_id, request_id), "files": items}

    def input_manifest(self, token):
        from src.worker.input_files import request_tag

        with self.database.connect() as db:
            chat, request = self.authorize(db, token)
            row = db.execute(
                "SELECT items FROM accepted_inputs WHERE chat_id=%s AND request_id=%s", (chat, request)
            ).fetchone()
            return {"request_tag": request_tag(chat, request), "files": json.loads(row[0]) if row else []}

    def accepted_input(self, token, identifier):
        manifest = self.input_manifest(token)
        if not any(item["artifact_id"] == identifier for item in manifest["files"]):
            raise PermissionError("Input was not selected for this request.")

    def lookup_input(self, chat_id, request_id, operation):
        identity(chat_id, request_id)
        operation_id(operation)
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id FROM artifacts WHERE chat_id=%s AND request_id=%s AND direction='input' AND operation=%s",
                (chat_id, request_id, operation),
            ).fetchone()
            return self.describe(row[0], chat_id, request_id, db=db) if row else None

    def begin_run(self, chat_id, request_id, seconds):
        identity(chat_id, request_id)
        if type(seconds) is not int or not 1 <= seconds <= 1830:
            raise ValueError("Invalid run lifetime.")
        token = secrets.token_urlsafe(32)
        with self.database.connect() as db:
            changed = db.execute(
                """INSERT INTO artifact_runs VALUES (%s,%s,'active',%s,%s)
                ON CONFLICT(chat_id,request_id) DO UPDATE SET state='active',capability=EXCLUDED.capability,deadline=EXCLUDED.deadline
                WHERE artifact_runs.state!='active' OR artifact_runs.deadline<=%s""",
                (chat_id, request_id, hashlib.sha256(token.encode()).hexdigest(), self.clock() + seconds, self.clock()),
            ).rowcount
            if changed != 1:
                raise BlockingIOError("Artifact run is already active.")
        return token

    def authorize(self, db, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise PermissionError("Invalid artifact capability.")
        row = db.execute(
            "SELECT chat_id,request_id FROM artifact_runs WHERE capability=%s AND state='active' AND deadline>%s FOR UPDATE",
            (hashlib.sha256(token.encode()).hexdigest(), self.clock()),
        ).fetchone()
        if row is None:
            raise PermissionError("Artifact capability expired or revoked.")
        return row

    def scope(self, token):
        with self.database.connect() as db:
            return self.authorize(db, token)

    def end_run(self, db, chat_id, request_id, state):
        if state not in ("completed", "failed", "cancelled"):
            raise ValueError("Invalid run state.")
        db.execute("UPDATE artifact_runs SET state=%s,capability=NULL WHERE chat_id=%s AND request_id=%s",
                   (state, chat_id, request_id))

    def cancel_run(self, chat_id, request_id):
        with self.database.connect() as db:
            db.execute("UPDATE artifact_runs SET state='cancelled',capability=NULL WHERE chat_id=%s AND request_id=%s AND state='active'",
                       (chat_id, request_id))

    def upload(self, chat_id, request_id, direction, operation, filename, mime, size, digest, source, capability=None):
        identity(chat_id, request_id)
        operation_id(operation)
        metadata(filename, mime, size, digest)
        maximum = self.policy.input_max_bytes if direction == "input" else self.policy.output_max_bytes
        if direction not in ("input", "output") or size > maximum:
            raise ValueError("Unsupported file size or direction.")
        now, artifact_id = self.clock(), uuid.uuid4().hex
        with self.database.connect() as db:
            # Capacity reservations serialize across uploads; transfer itself holds no database lock.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended('artifact-capacity',0))")
            if capability is not None and self.authorize(db, capability) != (chat_id, request_id):
                raise PermissionError("Wrong artifact request.")
            existing = db.execute("""SELECT id,filename,mime,size,digest,state FROM artifacts
                WHERE chat_id=%s AND request_id=%s AND direction=%s AND operation=%s""",
                (chat_id, request_id, direction, operation)).fetchone()
            if existing:
                if existing[1:5] != (filename, mime, size, digest):
                    raise ValueError("Upload identity already used.")
                if existing[5] not in ("prepared", "committed"):
                    historical = db.execute("""SELECT 1 FROM attachment_batches WHERE chat_id=%s AND request_id=%s
                        AND items::jsonb @> %s::jsonb AND version <
                        (SELECT max(version) FROM attachment_batches WHERE chat_id=%s AND request_id=%s) LIMIT 1""",
                        (chat_id, request_id, json.dumps([{"artifact_id": existing[0]}]), chat_id, request_id)).fetchone()
                    if historical and existing[5] in ("discarded", "expired"):
                        return {"artifact_id": existing[0], "state": "superseded"}
                    raise ValueError("Upload is incomplete or expired.")
                return self.describe(existing[0], chat_id, request_id, db=db)
            count = db.execute("SELECT count(*) FROM artifacts WHERE chat_id=%s AND request_id=%s AND direction=%s AND state!='expired'",
                               (chat_id, request_id, direction)).fetchone()[0]
            used = db.execute("SELECT COALESCE(sum(size),0) FROM artifacts WHERE state!='expired'").fetchone()[0]
            # Replacement retains one current batch while one candidate batch uploads.
            count_limit = self.policy.max_files * (2 if direction == "output" else 1)
            if count >= count_limit or used + size > self.policy.capacity_bytes:
                raise ArtifactCapacityError("Attachment storage limit reached.")
            db.execute("""INSERT INTO artifacts
                (id,chat_id,request_id,direction,operation,filename,mime,size,digest,state,created,expires)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'uploading',%s,%s)""",
                       (artifact_id, chat_id, request_id, direction, operation, filename, mime, size, digest,
                        now, now + self.policy.staging_seconds))
        temporary, target = self.root / (artifact_id + ".upload"), self.root / artifact_id
        try:
            computed, remaining = hashlib.sha256(), size
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as file:
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk or len(chunk) > remaining:
                        raise ValueError("Incomplete attachment upload.")
                    file.write(chunk)
                    computed.update(chunk)
                    remaining -= len(chunk)
                if computed.hexdigest() != digest:
                    raise ValueError("Attachment digest mismatch.")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
            if os.name != "nt":
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            with self.database.connect() as db:
                if capability is not None and self.authorize(db, capability) != (chat_id, request_id):
                    raise PermissionError("Artifact run ended during upload.")
                updated = db.execute("UPDATE artifacts SET state='prepared' WHERE id=%s AND state='uploading' AND expires>%s",
                                     (artifact_id, self.clock())).rowcount
                if updated != 1:
                    raise ValueError("Attachment upload expired.")
            return self.describe(artifact_id, chat_id, request_id)
        except BaseException:
            # A lost database commit acknowledgement must not remove a valid snapshot.
            # If metadata cannot be checked, maintenance reconciles it after the grace period.
            try:
                with self.database.connect() as db:
                    row = db.execute("SELECT state FROM artifacts WHERE id=%s FOR UPDATE", (artifact_id,)).fetchone()
                    if not row or row[0] in ("uploading", "expired"):
                        temporary.unlink(missing_ok=True)
                        target.unlink(missing_ok=True)
                        db.execute("DELETE FROM artifacts WHERE id=%s", (artifact_id,))
            except Exception:
                pass
            raise

    def describe(self, artifact_id, chat_id, request_id=None, db=None, committed=False):
        opaque_id(artifact_id)
        identity(chat_id, request_id or "0")
        if db is None:
            with self.database.connect() as connection:
                return self.describe(artifact_id, chat_id, request_id, connection, committed)
        row = db.execute("SELECT request_id,direction,filename,mime,size,digest,state,expires,presentation_mime FROM artifacts WHERE id=%s AND chat_id=%s",
                         (artifact_id, chat_id)).fetchone()
        if (not row or request_id is not None and row[0] != request_id or row[6] not in ("prepared", "committed")
                or committed and row[6] != "committed" or row[7] <= self.clock()):
            raise PermissionError("Attachment unavailable or expired.")
        return {"artifact_id": artifact_id, "request_id": row[0], "direction": row[1], "filename": row[2], "mime": row[3],
                "size": row[4], "sha256": row[5], "state": row[6], "expires_at": row[7], "presentation_mime": row[8]}

    def open(self, artifact_id, chat_id, request_id=None, committed=False):
        info = self.describe(artifact_id, chat_id, request_id, committed=committed)
        descriptor = os.open(self.root / opaque_id(artifact_id), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        file = os.fdopen(descriptor, "rb")
        details = os.fstat(file.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size != info["size"]:
            file.close()
            raise ValueError("Attachment snapshot unavailable.")
        return info, file

    def batch_lookup(self, capability, expected_version, request_key):
        operation_id(request_key)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Invalid batch version.")
        with self.database.connect() as db:
            chat_id, request_id = self.authorize(db, capability)
            row = db.execute("""SELECT items,version FROM attachment_batches WHERE chat_id=%s AND request_id=%s
                AND version=%s AND request_key=%s""", (chat_id, request_id, expected_version + 1, request_key)).fetchone()
            if row is None:
                return None
            current = db.execute("SELECT max(version) FROM attachment_batches WHERE chat_id=%s AND request_id=%s", (chat_id, request_id)).fetchone()[0]
            return {"state": "prepared" if row[1] == current else "superseded", "version": row[1], "current_version": current, "items": json.loads(row[0])}

    def prepare_batch(self, capability, operation, items, expected_version=0, request_key=None):
        operation_id(operation)
        if request_key is not None:
            operation_id(request_key)
        if type(expected_version) is not int or expected_version < 0 or not items and expected_version == 0:
            raise ValueError("A current version is required to clear attachments.")
        items = validate_items(items) if items else []
        if len(items) > self.policy.max_files or any(i["type"] == "text" for i in items):
            raise ValueError("Attachment batch required.")
        if len({i["artifact_id"] for i in items}) != len(items):
            raise ValueError("Duplicate attachment.")
        with self.database.connect() as db:
            chat_id, request_id = self.authorize(db, capability)
            existing = db.execute("SELECT items,version FROM attachment_batches WHERE chat_id=%s AND request_id=%s AND operation=%s",
                                  (chat_id, request_id, operation)).fetchone()
            current = db.execute("SELECT COALESCE(max(version),0) FROM attachment_batches WHERE chat_id=%s AND request_id=%s", (chat_id, request_id)).fetchone()[0]
            if existing:
                if json.loads(existing[0]) != items or existing[1] != expected_version + 1:
                    raise ValueError("Batch identity already used.")
                return {"state": "prepared" if existing[1] == current else "superseded", "version": existing[1], "current_version": current, "items": items}
            if expected_version != current:
                raise ValueError(f"Attachment version conflict; current version is {current}.")
            for item in items:
                info = self.describe(item["artifact_id"], chat_id, request_id, db=db)
                if info["direction"] != "output":
                    raise PermissionError("Output attachment required.")
                if item["type"] == "image":
                    if info["size"] > self.policy.image_max_bytes:
                        raise ValueError("Unsupported image size.")
                    from .image_validation import validate_image
                    descriptor = os.open(self.root / info["artifact_id"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(descriptor, "rb") as file:
                        mime = validate_image(file, info["mime"])
                    db.execute("UPDATE artifacts SET presentation_mime=%s WHERE id=%s", (mime, info["artifact_id"]))
            db.execute("INSERT INTO attachment_batches (chat_id,request_id,operation,items,version,request_key) VALUES (%s,%s,%s,%s,%s,%s)",
                       (chat_id, request_id, operation, json.dumps(items, ensure_ascii=False), current + 1, request_key))
            active = [item["artifact_id"] for item in items]
            # Discarded bytes remain charged until maintenance actually unlinks them.
            db.execute("""UPDATE artifacts SET state='discarded',expires=%s WHERE chat_id=%s AND request_id=%s
                AND direction='output' AND state='prepared' AND NOT (id=ANY(%s))""", (self.clock(), chat_id, request_id, active))
            return {"state": "prepared", "version": current + 1, "current_version": current + 1, "items": items}

    def publish(self, db, chat_id, request_id):
        run = db.execute("SELECT state,deadline FROM artifact_runs WHERE chat_id=%s AND request_id=%s FOR UPDATE", (chat_id, request_id)).fetchone()
        if not run or run[0] != "active" or run[1] <= self.clock():
            raise PermissionError("Artifact run no longer active.")
        rows = db.execute("SELECT items FROM attachment_batches WHERE chat_id=%s AND request_id=%s ORDER BY version DESC LIMIT 1", (chat_id, request_id)).fetchall()
        items = json.loads(rows[0][0]) if rows else []
        for item in items:
            self.describe(item["artifact_id"], chat_id, request_id, db=db)
            db.execute("UPDATE artifacts SET state='committed',expires=%s WHERE id=%s", (self.clock() + self.policy.retention_seconds, item["artifact_id"]))
        self.end_run(db, chat_id, request_id, "completed")
        return items

    def maintain(self):
        now = self.clock()
        with self.database.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended('artifact-capacity',0))")
            db.execute("UPDATE artifact_runs SET state='cancelled',capability=NULL WHERE state='active' AND deadline<=%s", (now,))
            expired = db.execute("UPDATE artifacts SET state='expired' WHERE expires<=%s AND state!='expired' RETURNING id", (now,)).fetchall()
            known = {row[0] for row in db.execute("SELECT id FROM artifacts WHERE state!='expired'")}
            for row in expired:
                (self.root / row[0]).unlink(missing_ok=True)
                (self.root / (row[0] + ".upload")).unlink(missing_ok=True)
            for path in self.root.iterdir():
                if path.name.removesuffix(".upload") not in known and path.lstat().st_mtime <= now - self.policy.staging_seconds:
                    if path.is_file() or path.is_symlink():
                        path.unlink()
