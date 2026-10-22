import json

from src.runtime import private_database


class DeliveryStore:
    def __init__(self, database):
        self.database = database
        with private_database(database) as db:
            db.execute("CREATE TABLE IF NOT EXISTS polling (id INTEGER PRIMARY KEY, offset INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO polling VALUES (1, 0)")
            db.execute("""CREATE TABLE IF NOT EXISTS pending (
                update_id INTEGER PRIMARY KEY, chat_id INTEGER, chunks TEXT NOT NULL, next_chunk INTEGER NOT NULL)""")

    @property
    def offset(self):
        with private_database(self.database) as db:
            return db.execute("SELECT offset FROM polling WHERE id = 1").fetchone()[0]

    def get(self, update_id):
        with private_database(self.database) as db:
            row = db.execute("SELECT chat_id, chunks, next_chunk FROM pending WHERE update_id = ?", (update_id,)).fetchone()
            return (row[0], json.loads(row[1]), row[2]) if row else None

    def prepare(self, update_id, chat_id, chunks):
        with private_database(self.database) as db:
            db.execute("INSERT INTO pending VALUES (?, ?, ?, 0)", (update_id, chat_id, json.dumps(chunks)))

    def mark_sent(self, update_id, next_chunk):
        with private_database(self.database) as db:
            db.execute("UPDATE pending SET next_chunk = ? WHERE update_id = ?", (next_chunk, update_id))

    def finish(self, update_id):
        with private_database(self.database) as db:
            db.execute("UPDATE polling SET offset = ? WHERE id = 1", (update_id + 1,))
            db.execute("DELETE FROM pending WHERE update_id = ?", (update_id,))
