"""Durable receipt cursor and bounded album jobs, independent from processing."""
import json
import time
import re

from .reply_context import extract_reply_context


def google_command(text):
    return (isinstance(text, str) and text.startswith("/")
            and text.split(maxsplit=1)[:1] in (["/google"], ["/calendars"], ["/google_callback"]))


def direct_control(message):
    return (type(message.get("chat", {}).get("id")) is int
            and message["chat"]["id"] > 0
            and type(message.get("from", {}).get("id")) is int
            and message["chat"]["id"] == message["from"]["id"]
            and not any(key in message for key in ("forward_origin", "forward_date", "forward_from", "forward_from_chat",
                                                   "quote", "external_reply", "reply_to_message", "via_bot")))


def normalize(update, allowed):
    callback = update.get("callback_query")
    if callback is not None:
        if not isinstance(callback, dict):
            return None
        message = callback.get("message")
        sender = callback.get("from")
        data, identifier = callback.get("data"), callback.get("id")
        if (not isinstance(message, dict) or not isinstance(sender, dict)
                or not isinstance(message.get("chat"), dict) or message["chat"].get("type") != "private"
                or str(sender.get("id")) not in allowed
                or type(sender.get("id")) is not int or sender["id"] != message["chat"].get("id")
                or type(message.get("message_id")) is not int or message["message_id"] <= 0
                or not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                or not isinstance(data, str) or not re.fullmatch(r"g:[A-Za-z0-9_-]{32}", data)):
            return None
        origin = {"chat": message["chat"], "from": sender}
        if not direct_control({**message, "from": sender}):
            return None
        return {"chat_id": str(sender["id"]), "text": "/google_callback " + data, "files": [], "error": None,
                "control_origin": origin, "callback": {"id": identifier, "data": data, "message_id": message["message_id"]}}
    message = update.get("message")
    if (
        not isinstance(message, dict)
        or not isinstance(message.get("chat"), dict)
        or message["chat"].get("type") != "private"
        or not isinstance(message.get("from"), dict)
        or str(message["from"].get("id")) not in allowed
    ):
        return None
    chat = message["chat"].get("id")
    if type(chat) is not int or chat <= 0:
        return None
    text = message.get("text", message.get("caption", ""))
    if google_command(text) and not direct_control(message):
        return None
    if not isinstance(text, str) or len(text) > 16000:
        return {"chat_id": str(chat), "text": "", "files": [], "error": "Message text is invalid or too long."}
    files = []
    if "photo" in message:
        photos = message["photo"]
        if isinstance(photos, list) and photos and all(isinstance(p, dict) for p in photos):
            photo = max(
                photos,
                key=lambda p: (p.get("width", 0) if type(p.get("width")) is int else 0)
                * (p.get("height", 0) if type(p.get("height")) is int else 0),
            )
            files = [
                {
                    "file_id": photo.get("file_id"),
                    "filename": "photo.jpg",
                    "mime": "image/jpeg",
                    "size": photo.get("file_size"),
                }
            ]
    elif "document" in message and isinstance(message["document"], dict):
        document = message["document"]
        files = [
            {
                "file_id": document.get("file_id"),
                "filename": document.get("file_name", "attachment.bin"),
                "mime": document.get("mime_type", "application/octet-stream"),
                "size": document.get("file_size"),
            }
        ]
    error = None
    if not files and not text.strip():
        error = "Send text, a photo, or a document. Audio and video interpretation are not available."
    for item in files:
        if (
            not isinstance(item["file_id"], str)
            or not 1 <= len(item["file_id"]) <= 1024
            or not isinstance(item["filename"], str)
            or len(item["filename"]) > 200
            or not isinstance(item["mime"], str)
            or len(item["mime"]) > 100
            or item["size"] is not None
            and (type(item["size"]) is not int or not 0 < item["size"] <= 20000000)
        ):
            error = "File unavailable or too large. Maximum 10 files and 20 MB combined."
            item.update(file_id="", filename="unavailable", mime="application/octet-stream", size=None)
    album = message.get("media_group_id")
    if album is not None and (not isinstance(album, str) or len(album) > 128):
        error = "Invalid media album."
        album = None
    item = {"chat_id": str(chat), "text": text, "files": files, "album": album, "error": error}
    if google_command(text):
        item["control_origin"] = {"chat": message["chat"], "from": message["from"]}
        if files or album:
            return None
    context = extract_reply_context(message)
    if context:
        item["reply_context"] = context
    return item


class Inbox:
    def __init__(self, database, clock=time.time, quiet=1, maximum_wait=3, max_jobs=1000, max_age=86400):
        self.database, self.clock, self.quiet, self.maximum_wait, self.max_jobs, self.max_age = (
            database,
            clock,
            quiet,
            maximum_wait,
            max_jobs,
            max_age,
        )
        with database.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS inbox_cursor (id INTEGER PRIMARY KEY, value BIGINT NOT NULL)")
            db.execute('INSERT INTO inbox_cursor SELECT 1,"offset" FROM polling WHERE id=1 ON CONFLICT DO NOTHING')
            db.execute(
                """CREATE TABLE IF NOT EXISTS inbox_jobs (id BIGINT PRIMARY KEY,chat_id TEXT NOT NULL,album TEXT,
                payload TEXT NOT NULL,state TEXT NOT NULL,created DOUBLE PRECISION NOT NULL,updated DOUBLE PRECISION NOT NULL)"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS inbox_jobs_state ON inbox_jobs(state,id)")

    def recover(self):
        # Called only after the process owns the exclusive dispatcher session lock.
        with self.database.connect() as db:
            db.execute("UPDATE inbox_jobs SET state='retry' WHERE state='running'")

    @property
    def offset(self):
        with self.database.connect() as db:
            return db.execute("SELECT value FROM inbox_cursor WHERE id=1").fetchone()[0]

    def receive(self, update, allowed):
        identifier = update.get("update_id")
        if type(identifier) is not int or identifier < 0:
            raise ValueError("Invalid Telegram update identity.")
        item = normalize(update, allowed)
        now = self.clock()
        with self.database.connect() as db:
            db.execute("SELECT value FROM inbox_cursor WHERE id=1 FOR UPDATE")
            if identifier < db.execute("SELECT value FROM inbox_cursor WHERE id=1").fetchone()[0]:
                return
            if item:
                pending = db.execute("SELECT count(*) FROM inbox_jobs WHERE state!='done'").fetchone()[0]
                if pending >= self.max_jobs:
                    raise BlockingIOError("Input queue is full.")
                row = None
                if item.get("album"):
                    row = db.execute(
                        "SELECT id,payload,state,created,updated FROM inbox_jobs WHERE chat_id=%s AND album=%s ORDER BY id LIMIT 1 FOR UPDATE",
                        (item["chat_id"], item["album"]),
                    ).fetchone()
                if row and row[2] == "open" and now < row[3] + self.maximum_wait:
                    payload = json.loads(row[1])
                    if len(payload["messages"]) < 10:
                        payload["messages"].append(item)
                    else:
                        payload["error"] = "Album exceeds ten attachments."
                    db.execute(
                        "UPDATE inbox_jobs SET payload=%s,updated=%s WHERE id=%s", (json.dumps(payload), now, row[0])
                    )
                else:
                    if row:
                        item["error"] = "Album already started. Send a new complete album or message."
                    payload = {"messages": [item], "uploads": {}, "error": item.get("error")}
                    db.execute(
                        "INSERT INTO inbox_jobs VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (
                            identifier,
                            item["chat_id"],
                            item.get("album") if not row else None,
                            json.dumps(payload),
                            "open" if item.get("album") and not row else "ready",
                            now,
                            now,
                        ),
                    )
            db.execute("UPDATE inbox_cursor SET value=%s WHERE id=1", (identifier + 1,))
            db.execute("DELETE FROM inbox_jobs WHERE state='done' AND updated<%s", (now - self.max_age,))

    def claim(self):
        now = self.clock()
        with self.database.connect() as db:
            db.execute(
                "UPDATE inbox_jobs SET state='ready' WHERE state='open' AND (updated<=%s OR created<=%s)",
                (now - self.quiet, now - self.maximum_wait),
            )
            row = db.execute(
                "SELECT id,chat_id,payload,created,state FROM inbox_jobs WHERE state IN ('open','ready','retry') ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
            ).fetchone()
            if not row:
                return None
            if row[4] == "open":
                return None
            payload = json.loads(row[2])
            if row[3] < now - self.max_age:
                payload["error"] = (
                    "Previous input remains uncertain and its files expired. Check effects before retrying."
                    if payload.get("inference_started")
                    else "Input request expired before processing. Please send it again."
                )
            db.execute("UPDATE inbox_jobs SET state='running' WHERE id=%s", (row[0],))
            return {"id": row[0], "chat_id": row[1], **payload}

    def failed_transfer(self, identifier):
        with self.database.connect() as db:
            row = db.execute("SELECT payload FROM inbox_jobs WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
            payload = json.loads(row[0])
            count = payload.get("transfer_failures", 0) + 1
            payload["transfer_failures"] = count
            db.execute("UPDATE inbox_jobs SET payload=%s WHERE id=%s", (json.dumps(payload), identifier))
            return count

    def mark_inference(self, identifier):
        with self.database.connect() as db:
            row = db.execute("SELECT payload FROM inbox_jobs WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
            payload = json.loads(row[0])
            payload["inference_started"] = True
            db.execute("UPDATE inbox_jobs SET payload=%s WHERE id=%s", (json.dumps(payload), identifier))

    def save_upload(self, identifier, index, artifact):
        with self.database.connect() as db:
            row = db.execute("SELECT payload FROM inbox_jobs WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
            payload = json.loads(row[0])
            payload["uploads"][str(index)] = artifact
            db.execute("UPDATE inbox_jobs SET payload=%s WHERE id=%s", (json.dumps(payload), identifier))

    def finish(self, identifier, retry=False):
        with self.database.connect() as db:
            db.execute(
                "UPDATE inbox_jobs SET state=%s,updated=%s WHERE id=%s",
                ("retry" if retry else "done", self.clock(), identifier),
            )
