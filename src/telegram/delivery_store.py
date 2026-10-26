import json



class DeliveryStore:
    def __init__(self, database):
        self.database = database
        with database.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS polling (id INTEGER PRIMARY KEY, "offset" BIGINT NOT NULL)')
            db.execute("INSERT INTO polling VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
            db.execute("""CREATE TABLE IF NOT EXISTS pending (
                update_id BIGINT PRIMARY KEY, chat_id BIGINT, chunks TEXT NOT NULL, next_chunk INTEGER NOT NULL)""")
            db.execute("CREATE TABLE IF NOT EXISTS uncertain_updates (update_id BIGINT PRIMARY KEY)")

    def uncertain(self, update_id):
        with self.database.connect() as db:
            return db.execute("SELECT 1 FROM uncertain_updates WHERE update_id=%s", (update_id,)).fetchone() is not None

    def mark_uncertain(self, update_id):
        with self.database.connect() as db:
            db.execute("INSERT INTO uncertain_updates VALUES (%s) ON CONFLICT DO NOTHING", (update_id,))

    @property
    def offset(self):
        with self.database.connect() as db:
            return db.execute('SELECT "offset" FROM polling WHERE id = 1').fetchone()[0]

    def get(self, update_id):
        with self.database.connect() as db:
            row = db.execute("SELECT chat_id, chunks, next_chunk FROM pending WHERE update_id = %s", (update_id,)).fetchone()
            return (row[0], json.loads(row[1]), row[2]) if row else None

    def prepare(self, update_id, chat_id, chunks):
        with self.database.connect() as db:
            db.execute("INSERT INTO pending VALUES (%s, %s, %s, 0)", (update_id, chat_id, json.dumps(chunks)))

    def mark_sent(self, update_id, next_chunk):
        with self.database.connect() as db:
            db.execute("UPDATE pending SET next_chunk = %s WHERE update_id = %s", (next_chunk, update_id))

    def finish(self, update_id):
        with self.database.connect() as db:
            db.execute('UPDATE polling SET "offset" = GREATEST("offset", %s) WHERE id = 1', (update_id + 1,))
            db.execute("DELETE FROM pending WHERE update_id = %s", (update_id,))
            db.execute("DELETE FROM uncertain_updates WHERE update_id=%s", (update_id,))
