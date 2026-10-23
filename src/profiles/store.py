import json

from .auth import ProfileTokens
from .instructions import DEFAULT_PROFILE, validate_changes


class ProfileStore:
    def __init__(self, database):
        self.database = database
        with database.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS profiles (
                chat_id TEXT PRIMARY KEY, version INTEGER NOT NULL, settings TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS profile_versions (
                chat_id TEXT, version INTEGER, settings TEXT NOT NULL,
                PRIMARY KEY (chat_id, version))""")

    def get(self, chat_id):
        ProfileTokens.validate_chat(chat_id)
        with self.database.connect() as db:
            row = db.execute("SELECT version, settings FROM profiles WHERE chat_id = %s", (chat_id,)).fetchone()
        return {**DEFAULT_PROFILE, **(json.loads(row[1]) if row else {}), "version": row[0] if row else 0}

    def update(self, chat_id, expected_version, changes):
        ProfileTokens.validate_chat(chat_id)
        changes = validate_changes(changes)
        defaults = DEFAULT_PROFILE
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("A nonnegative profile version is required.")
        with self.database.connect() as db:
            # Lock first writes too: a nonexistent profile cannot be locked with FOR UPDATE.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (chat_id,))
            row = db.execute("SELECT version, settings FROM profiles WHERE chat_id = %s", (chat_id,)).fetchone()
            version = row[0] if row else 0
            if version != expected_version:
                raise ValueError("Profile changed. Read the profile again before updating.")
            overrides = {**(json.loads(row[1]) if row else {}), **changes}
            settings = {**defaults, **overrides}
            encoded = json.dumps(overrides)
            # Keep the initial state as well as subsequent versions for operator recovery.
            if row is None:
                db.execute("INSERT INTO profile_versions VALUES (%s, 0, %s)", (chat_id, json.dumps(defaults)))
            db.execute("""INSERT INTO profiles VALUES (%s, %s, %s) ON CONFLICT (chat_id)
                DO UPDATE SET version = EXCLUDED.version, settings = EXCLUDED.settings""", (chat_id, version + 1, encoded))
            db.execute("INSERT INTO profile_versions VALUES (%s, %s, %s)", (chat_id, version + 1, json.dumps(settings)))
            # Bound retained private history and disk growth per chat.
            db.execute("DELETE FROM profile_versions WHERE chat_id = %s AND version < %s", (chat_id, max(0, version - 18)))
        return {**settings, "version": version + 1}
