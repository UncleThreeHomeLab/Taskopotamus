"""One durable owner of outgoing replies and external-send uncertainty."""
import json
import re
import time
import uuid

from .messages import delivery_items, identity, opaque_id, validate_items


class DeliveryStore:
    def __init__(self, database, retention=604800, attempt_timeout=180, clock=time.time):
        if type(retention) is not int or retention <= 0 or type(attempt_timeout) is not int or attempt_timeout <= 0:
            raise ValueError("Invalid delivery policy.")
        self.database, self.retention, self.attempt_timeout, self.clock = database, retention, attempt_timeout, clock
        with database.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS outgoing_replies (
                id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, request_id TEXT NOT NULL,
                channel TEXT NOT NULL, items TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,
                expires DOUBLE PRECISION NOT NULL, UNIQUE(chat_id, request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS outgoing_items (
                reply_id TEXT REFERENCES outgoing_replies(id), sequence INTEGER NOT NULL,
                item TEXT NOT NULL, state TEXT NOT NULL, attempt TEXT, claimed DOUBLE PRECISION,
                receipt TEXT, PRIMARY KEY(reply_id, sequence))""")
            db.execute("CREATE INDEX IF NOT EXISTS outgoing_items_state ON outgoing_items(state)")
            db.execute("ALTER TABLE outgoing_replies ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'completed'")
            db.execute("ALTER TABLE outgoing_items ADD COLUMN IF NOT EXISTS not_before DOUBLE PRECISION NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE outgoing_items ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE TABLE IF NOT EXISTS delivery_pacing (chat_id TEXT PRIMARY KEY, not_before DOUBLE PRECISION NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS delivery_attempts (
                attempt TEXT PRIMARY KEY, reply_id TEXT NOT NULL, sequence INTEGER NOT NULL, state TEXT NOT NULL, receipt TEXT)""")
            db.execute("""INSERT INTO delivery_attempts SELECT attempt,reply_id,sequence,state,receipt FROM outgoing_items
                WHERE attempt IS NOT NULL ON CONFLICT DO NOTHING""")
            db.execute("""CREATE TABLE IF NOT EXISTS local_commands (
                chat_id TEXT, request_id TEXT, command TEXT NOT NULL, attempt TEXT NOT NULL,
                state TEXT NOT NULL, PRIMARY KEY(chat_id,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS outgoing_batches (
                reply_id TEXT REFERENCES outgoing_replies(id), message_key TEXT NOT NULL, items TEXT NOT NULL,
                PRIMARY KEY(reply_id,message_key))""")
            db.execute("ALTER TABLE outgoing_batches ADD COLUMN IF NOT EXISTS request_signature TEXT")

    def reserve_command(self, chat_id, request_id, command):
        identity(chat_id, request_id)
        if not isinstance(command, str) or not command.startswith("/") or len(command) > 16000:
            raise ValueError("Invalid local command.")
        with self.database.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("command:" + chat_id + ":" + request_id,))
            previous = db.execute("SELECT command FROM local_commands WHERE chat_id=%s AND request_id=%s FOR UPDATE",
                                  (chat_id, request_id)).fetchone()
            if previous and previous[0] != command:
                raise ValueError("Command identity already used.")
            reply = self.get(chat_id, request_id, db)
            if reply:
                return {"reply": reply, "attempt": None}
            if previous:
                # A prior caller may have performed the action before losing its acknowledgement.
                notice = "Previous command outcome is uncertain. Check current state before sending the command again."
                self.publish(db, chat_id, request_id, [{"type": "text", "text": notice}], outcome="failed")
                db.execute("UPDATE local_commands SET state='uncertain' WHERE chat_id=%s AND request_id=%s", (chat_id, request_id))
                return {"reply": self.get(chat_id, request_id, db), "attempt": None}
            attempt = uuid.uuid4().hex
            db.execute("INSERT INTO local_commands VALUES (%s,%s,%s,%s,'reserved')", (chat_id, request_id, command, attempt))
            return {"reply": None, "attempt": attempt}

    def complete_command(self, chat_id, request_id, attempt, text):
        identity(chat_id, request_id)
        opaque_id(attempt)
        with self.database.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("command:" + chat_id + ":" + request_id,))
            row = db.execute("SELECT attempt,state FROM local_commands WHERE chat_id=%s AND request_id=%s FOR UPDATE",
                             (chat_id, request_id)).fetchone()
            if not row or row[0] != attempt:
                raise ValueError("Invalid local command attempt.")
            existing = self.get(chat_id, request_id, db)
            if existing:
                return existing
            if row[1] != "reserved":
                raise ValueError("Local command no longer active.")
            self.publish(db, chat_id, request_id, [{"type": "text", "text": text}])
            db.execute("UPDATE local_commands SET state='completed' WHERE chat_id=%s AND request_id=%s", (chat_id, request_id))
            return self.get(chat_id, request_id, db)

    @staticmethod
    def _lock(db, chat_id, request_id):
        identity(chat_id, request_id)
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("outgoing:" + chat_id + ":" + request_id,))

    def append(self, db, chat_id, request_id, message_key, items, request_signature=None):
        if not isinstance(message_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", message_key):
            raise ValueError("Invalid message identity.")
        return self._append(db, chat_id, request_id, message_key, items, request_signature=request_signature)

    def _append(self, db, chat_id, request_id, message_key, items, terminal=False, legacy_next=None, request_signature=None):
        channel, items = identity(chat_id, request_id), validate_items(items)
        self._lock(db, chat_id, request_id)
        existing = self.snapshot(chat_id, request_id, db)
        if existing:
            prior = db.execute("SELECT items,request_signature FROM outgoing_batches WHERE reply_id=%s AND message_key=%s",
                               (existing["delivery_id"], message_key)).fetchone()
            if prior:
                if json.loads(prior[0]) != items or prior[1] != request_signature:
                    raise ValueError("Message identity already used.")
                return existing["delivery_id"]
            if existing["reply_status"] != "pending" or existing["expires_at"] <= self.clock() and not terminal:
                raise ValueError("Reply is closed or expired.")
        combined = validate_items((existing["items"] if existing else []) + items)
        # Leave one application-only text item available for a truthful failure or interruption notice.
        if not terminal and (len(combined) > 31 or sum(len(i["text"]) for i in combined if i["type"] == "text") > 62976):
            raise ValueError("Message limit reached; terminal notice capacity is reserved.")
        for item in items:
            if item["type"] in ("image", "file"):
                available = db.execute("""SELECT 1 FROM artifacts WHERE id=%s AND chat_id=%s AND request_id=%s
                    AND direction='output' AND state='committed' AND expires>%s""",
                    (item["artifact_id"], chat_id, request_id, self.clock())).fetchone()
                if not available:
                    raise ValueError("Committed output attachment required.")
        serialized = json.dumps(items, ensure_ascii=False)
        if existing:
            reply_id = existing["delivery_id"]
            db.execute("UPDATE outgoing_replies SET items=%s WHERE id=%s", (json.dumps(combined, ensure_ascii=False), reply_id))
        else:
            reply_id, now = uuid.uuid4().hex, self.clock()
            db.execute("""INSERT INTO outgoing_replies (id,chat_id,request_id,channel,items,created,expires,outcome)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')""",
                (reply_id, chat_id, request_id, channel, serialized, now, now + self.retention))
        offset = db.execute("SELECT COALESCE(max(sequence)+1,0) FROM outgoing_items WHERE reply_id=%s", (reply_id,)).fetchone()[0]
        for sequence, item in enumerate(delivery_items(items), offset):
            state, receipt = ("available" if channel == "console" else "pending"), None
            if legacy_next is not None:
                state = "delivered" if sequence < legacy_next else "uncertain" if sequence == legacy_next else "pending"
                receipt = json.dumps({"legacy": True}) if sequence < legacy_next else None
            db.execute("""INSERT INTO outgoing_items (reply_id,sequence,item,state,attempt,claimed,receipt)
                VALUES (%s,%s,%s,%s,NULL,NULL,%s)""",
                       (reply_id, sequence, json.dumps(item, ensure_ascii=False), state, receipt))
        db.execute("INSERT INTO outgoing_batches (reply_id,message_key,items,request_signature) VALUES (%s,%s,%s,%s)",
                   (reply_id, message_key, serialized, request_signature))
        return reply_id

    def message_lookup(self, db, chat_id, request_id, message_key, request_signature):
        row = db.execute("""SELECT r.id,b.items,b.request_signature FROM outgoing_batches b JOIN outgoing_replies r
            ON r.id=b.reply_id WHERE r.chat_id=%s AND r.request_id=%s AND b.message_key=%s""",
            (chat_id, request_id, message_key)).fetchone()
        if row is None:
            return None
        if row[2] != request_signature:
            raise ValueError("Message identity already used for different arguments.")
        return {"state": "queued", "delivery_id": row[0], "items": json.loads(row[1])}

    def finish(self, db, chat_id, request_id, outcome="completed", items=None, legacy_next=None):
        if outcome not in ("completed", "failed"):
            raise ValueError("Invalid reply outcome.")
        self._lock(db, chat_id, request_id)
        existing = self.snapshot(chat_id, request_id, db)
        if existing and existing["reply_status"] != "pending":
            prior = db.execute("SELECT items FROM outgoing_batches WHERE reply_id=%s AND message_key='application:terminal'",
                               (existing["delivery_id"],)).fetchone()
            expected = json.loads(prior[0]) if prior else existing["items"]
            if existing["reply_status"] != outcome or items is not None and validate_items(items) != expected:
                raise ValueError("Reply identity already used.")
            return existing["delivery_id"]
        expired = existing is not None and existing["expires_at"] <= self.clock()
        if expired and outcome != "failed":
            raise ValueError("Expired requests must finish as failed.")
        if items is not None:
            reply_id = self._append(db, chat_id, request_id, "application:terminal", items, terminal=True, legacy_next=legacy_next)
        elif existing:
            reply_id = existing["delivery_id"]
        else:
            raise ValueError("A reply must contain a message.")
        db.execute("UPDATE outgoing_replies SET outcome=%s WHERE id=%s", (outcome, reply_id))
        if expired:
            # Record the failure without renewing expired messages or changing existing send receipts.
            db.execute("UPDATE outgoing_items SET state='expired' WHERE reply_id=%s AND state IN ('pending','available')",
                       (reply_id,))
        return reply_id

    def publish(self, db, chat_id, request_id, items, legacy_next=None, outcome="completed"):
        return self.finish(db, chat_id, request_id, outcome, items, legacy_next)

    def get(self, chat_id, request_id, db=None):
        result = self.snapshot(chat_id, request_id, db)
        return result if result and result["reply_status"] != "pending" else None

    def snapshot(self, chat_id, request_id, db=None):
        identity(chat_id, request_id)
        if db is None:
            with self.database.connect() as connection:
                return self.snapshot(chat_id, request_id, connection)
        row = db.execute("SELECT id,items,expires,outcome FROM outgoing_replies WHERE chat_id=%s AND request_id=%s",
                         (chat_id, request_id)).fetchone()
        if row is None:
            return None
        return {"delivery_id": row[0], "items": json.loads(row[1]), "expires_at": row[2], "reply_status": row[3]}

    def maintain(self, db):
        now = self.clock()
        db.execute("UPDATE outgoing_items SET state='uncertain' WHERE state='inflight' AND claimed <= %s",
                   (now - self.attempt_timeout,))
        db.execute("""UPDATE delivery_attempts a SET state='uncertain' WHERE a.state='inflight' AND EXISTS
            (SELECT 1 FROM outgoing_items i WHERE i.attempt=a.attempt AND i.state='uncertain')""")
        db.execute("""UPDATE outgoing_items SET state='expired' WHERE state IN ('pending','available') AND reply_id IN
                   (SELECT id FROM outgoing_replies WHERE expires <= %s)""", (now,))

    def claim(self, allowed_chats):
        if not isinstance(allowed_chats, list) or len(allowed_chats) > 100:
            raise ValueError("Invalid allowed recipients.")
        for chat in allowed_chats:
            if identity(chat, "0") != "telegram":
                raise ValueError("Telegram recipient required.")
        with self.database.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended('delivery-claims',0))")
            self.maintain(db)
            row = db.execute("""SELECT i.reply_id,i.sequence,i.item,r.chat_id,r.request_id FROM outgoing_items i
                JOIN outgoing_replies r ON r.id=i.reply_id WHERE i.state='pending' AND r.channel='telegram'
                AND r.chat_id=ANY(%s) AND r.expires>%s AND i.not_before<=%s
                AND NOT EXISTS (SELECT 1 FROM delivery_pacing p WHERE p.chat_id=r.chat_id AND p.not_before>%s)
                AND NOT EXISTS (SELECT 1 FROM outgoing_items busy JOIN outgoing_replies br ON br.id=busy.reply_id
                                WHERE br.chat_id=r.chat_id AND busy.state='inflight') AND NOT EXISTS
                  (SELECT 1 FROM outgoing_items earlier WHERE earlier.reply_id=i.reply_id
                   AND earlier.sequence<i.sequence AND earlier.state!='delivered')
                ORDER BY r.created,i.sequence LIMIT 1 FOR UPDATE OF i SKIP LOCKED""",
                (allowed_chats, self.clock(), self.clock(), self.clock())).fetchone()
            if row is None:
                return None
            attempt = uuid.uuid4().hex
            item = json.loads(row[2])
            if item["type"] in ("image", "file"):
                # Pin bytes through this bounded send attempt, including claims just before expiry.
                pinned = db.execute("""UPDATE artifacts SET expires=GREATEST(expires,%s)
                    WHERE id=%s AND chat_id=%s AND state='committed' AND expires>%s""",
                    (self.clock() + self.attempt_timeout, item["artifact_id"], row[3], self.clock())).rowcount
                if pinned != 1:
                    db.execute("UPDATE outgoing_items SET state='expired' WHERE reply_id=%s AND sequence=%s", (row[0], row[1]))
                    return None
            db.execute("UPDATE outgoing_items SET state='inflight',attempt=%s,claimed=%s WHERE reply_id=%s AND sequence=%s",
                       (attempt, self.clock(), row[0], row[1]))
            db.execute("INSERT INTO delivery_attempts VALUES (%s,%s,%s,'inflight',NULL)", (attempt, row[0], row[1]))
            db.execute("""INSERT INTO delivery_pacing VALUES (%s,%s) ON CONFLICT(chat_id)
                DO UPDATE SET not_before=EXCLUDED.not_before""", (row[3], self.clock() + 1))
            return {"delivery_id": row[0], "sequence": row[1], "item": item,
                    "chat_id": row[3], "request_id": row[4], "attempt": attempt}

    def acknowledge(self, reply_id, sequence, attempt, state, receipt=None):
        opaque_id(reply_id)
        opaque_id(attempt)
        if type(sequence) is not int or sequence < 0 or state not in ("delivered", "failed", "uncertain", "rate_limited"):
            raise ValueError("Invalid delivery receipt.")
        if receipt is not None and (not isinstance(receipt, dict) or len(json.dumps(receipt)) > 2048):
            raise ValueError("Invalid delivery receipt.")
        if state == "delivered" and (not receipt or type(receipt.get("message_id")) is not int or receipt["message_id"] <= 0):
            raise ValueError("Confirmed delivery needs a message receipt.")
        if state == "rate_limited" and (not receipt or set(receipt) != {"retry_after"}
                                       or type(receipt["retry_after"]) is not int or not 1 <= receipt["retry_after"] <= 300):
            raise ValueError("Invalid retry delay.")
        with self.database.connect() as db:
            row = db.execute("SELECT state,attempt,receipt,retry_count FROM outgoing_items WHERE reply_id=%s AND sequence=%s FOR UPDATE",
                             (reply_id, sequence)).fetchone()
            if not row:
                raise ValueError("Stale delivery attempt.")
            history = db.execute("SELECT reply_id,sequence,state,receipt FROM delivery_attempts WHERE attempt=%s FOR UPDATE", (attempt,)).fetchone()
            if not history or history[:2] != (reply_id, sequence):
                raise ValueError("Stale delivery attempt.")
            prior_receipt = json.loads(history[3]) if history[3] else None
            if history[2] == state and prior_receipt == receipt:
                return
            if history[2] != "inflight" and not (history[2] == "uncertain" and prior_receipt is None and state == "delivered"):
                raise ValueError("Delivery attempt already resolved.")
            if row[1] != attempt:
                raise ValueError("Stale delivery attempt.")
            if row[0] != "inflight" and not (row[0] == "uncertain" and row[2] is None and state == "delivered"):
                if row[0] == state and (json.loads(row[2]) if row[2] else None) == receipt:
                    return
                raise ValueError("Delivery attempt already resolved.")
            serialized = json.dumps(receipt) if receipt else None
            db.execute("UPDATE delivery_attempts SET state=%s,receipt=%s WHERE attempt=%s", (state, serialized, attempt))
            if state == "rate_limited" and row[3] < 3:
                db.execute("""UPDATE outgoing_items SET state='pending',attempt=NULL,claimed=NULL,receipt=NULL,
                    retry_count=retry_count+1,not_before=%s WHERE reply_id=%s AND sequence=%s""",
                    (self.clock() + receipt["retry_after"], reply_id, sequence))
            else:
                db.execute("UPDATE outgoing_items SET state=%s,receipt=%s WHERE reply_id=%s AND sequence=%s",
                           ("failed" if state == "rate_limited" else state, serialized, reply_id, sequence))
            cooldown = receipt["retry_after"] if state == "rate_limited" else 1
            db.execute("""UPDATE delivery_pacing SET not_before=GREATEST(not_before,%s) WHERE chat_id=
                (SELECT chat_id FROM outgoing_replies WHERE id=%s)""", (self.clock() + cooldown, reply_id))

    def status(self, chat_id):
        identity(chat_id, "0")
        with self.database.connect() as db:
            self.maintain(db)
            rows = db.execute("""SELECT r.request_id,i.sequence,i.state FROM outgoing_replies r JOIN outgoing_items i
                ON r.id=i.reply_id WHERE r.chat_id=%s ORDER BY r.created DESC,i.sequence LIMIT 32""", (chat_id,)).fetchall()
        return [{"request_id": row[0], "sequence": row[1], "state": row[2]} for row in rows]

    def import_legacy(self, chat_id, request_id, chunks, next_chunk):
        identity(chat_id, request_id)
        if not isinstance(chunks, list) or not chunks or any(not isinstance(c, str) or not c or len(c) > 2000 for c in chunks):
            raise ValueError("Invalid legacy reply.")
        if type(next_chunk) is not int or not 0 <= next_chunk <= len(chunks):
            raise ValueError("Invalid legacy progress.")
        with self.database.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("legacy:" + chat_id + ":" + request_id,))
            return self.publish(db, chat_id, request_id, [{"type": "text", "text": "".join(chunks)}], next_chunk)
