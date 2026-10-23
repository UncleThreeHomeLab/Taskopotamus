import json


def normalize_chunks(chunks):
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("Invalid saved Telegram delivery.")
    normalized = []
    for chunk in chunks:
        if isinstance(chunk, str):
            chunk = {"text": chunk, "plain_text": chunk, "parse_mode": None, "state": "pending"}
        if not isinstance(chunk, dict) or not {"text", "plain_text", "parse_mode", "state"} <= chunk.keys() or chunk.keys() - {"text", "plain_text", "parse_mode", "state", "reason"}:
            raise ValueError("Invalid saved Telegram delivery.")
        if chunk["parse_mode"] not in (None, "HTML") or chunk["state"] not in ("pending", "sending", "uncertain", "rejected"):
            raise ValueError("Invalid saved Telegram delivery.")
        if any(not isinstance(chunk[key], str) or not chunk[key] for key in ("text", "plain_text")):
            raise ValueError("Invalid saved Telegram delivery.")
        if len(chunk["text"]) > 262144 or len(chunk["plain_text"].encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Invalid saved Telegram delivery.")
        if chunk["parse_mode"] is None and chunk["text"] != chunk["plain_text"]:
            raise ValueError("Invalid saved Telegram delivery.")
        if "reason" in chunk and chunk["reason"] not in ("network_outcome_unknown", "recovered_inflight", "telegram_rejected"):
            raise ValueError("Invalid saved Telegram delivery.")
        normalized.append(dict(chunk))
    return normalized

class DeliveryStore:
    def __init__(self, database):
        self.database = database
        with database.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS polling (id INTEGER PRIMARY KEY, "offset" BIGINT NOT NULL)')
            db.execute("INSERT INTO polling VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
            db.execute("""CREATE TABLE IF NOT EXISTS pending (
                update_id BIGINT PRIMARY KEY, chat_id BIGINT, chunks TEXT NOT NULL, next_chunk INTEGER NOT NULL)""")

    @property
    def offset(self):
        with self.database.connect() as db:
            return db.execute('SELECT "offset" FROM polling WHERE id = 1').fetchone()[0]

    def get(self, update_id):
        with self.database.connect() as db:
            row = db.execute("SELECT chat_id, chunks, next_chunk FROM pending WHERE update_id = %s", (update_id,)).fetchone()
            if not row:
                return None
            original = json.loads(row[1])
            chunks = normalize_chunks(original)
            if type(row[2]) is not int or not 0 <= row[2] <= len(chunks):
                raise ValueError("Invalid saved Telegram progress.")
            if chunks != original:
                db.execute("UPDATE pending SET chunks = %s WHERE update_id = %s", (json.dumps(chunks), update_id))
            return row[0], chunks, row[2]

    def prepare(self, update_id, chat_id, chunks):
        chunks = normalize_chunks(chunks)
        with self.database.connect() as db:
            db.execute("INSERT INTO pending VALUES (%s, %s, %s, 0)", (update_id, chat_id, json.dumps(chunks)))

    def mark_sent(self, update_id, next_chunk):
        with self.database.connect() as db:
            db.execute("UPDATE pending SET next_chunk = %s WHERE update_id = %s", (next_chunk, update_id))

    def replace_chunk(self, update_id, index, chunk):
        with self.database.connect() as db:
            row = db.execute("SELECT chunks, next_chunk FROM pending WHERE update_id = %s FOR UPDATE", (update_id,)).fetchone()
            if row is None or row[1] != index:
                raise ValueError("Invalid saved Telegram progress.")
            chunks = normalize_chunks(json.loads(row[0]))
            chunks[index] = normalize_chunks([chunk])[0]
            db.execute("UPDATE pending SET chunks = %s WHERE update_id = %s", (json.dumps(chunks), update_id))

    def mark_sending(self, update_id, index, chunk):
        self.replace_chunk(update_id, index, {**chunk, "state": "sending"})

    def quarantine(self, update_id, index, state, reason):
        if state not in ("uncertain", "rejected"):
            raise ValueError("Invalid quarantine state.")
        with self.database.connect() as db:
            row = db.execute("SELECT chunks, next_chunk FROM pending WHERE update_id = %s FOR UPDATE", (update_id,)).fetchone()
            if row is None or row[1] != index:
                raise ValueError("Invalid saved Telegram progress.")
            chunks = normalize_chunks(json.loads(row[0]))
            chunks[index] = {**chunks[index], "state": state, "reason": reason}
            normalize_chunks(chunks)
            db.execute("UPDATE pending SET chunks = %s WHERE update_id = %s", (json.dumps(chunks), update_id))
            # Acknowledged for polling is not a claim that this retained message was delivered.
            db.execute('UPDATE polling SET "offset" = GREATEST("offset", %s) WHERE id = 1', (update_id + 1,))

    def finish(self, update_id):
        with self.database.connect() as db:
            db.execute('UPDATE polling SET "offset" = GREATEST("offset", %s) WHERE id = 1', (update_id + 1,))
            db.execute("DELETE FROM pending WHERE update_id = %s", (update_id,))
