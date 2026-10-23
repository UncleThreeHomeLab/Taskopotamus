import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from psycopg import sql

from .database import Database


TABLES = {
    "agent": {
        "conversations": ("chat_id", "thread_id"),
        "thread_context": ("thread_id", "model", "context_window"),
        "replies": ("chat_id", "request_id", "input_hash", "reply"),
    },
    "profiles": {
        "profiles": ("chat_id", "version", "settings"),
        "profile_versions": ("chat_id", "version", "settings"),
    },
    "telegram": {
        "polling": ("id", "offset"),
        "pending": ("update_id", "chat_id", "chunks", "next_chunk"),
    },
}


def rows(cursor):
    while batch := cursor.fetchmany(500):
        yield from batch


def fingerprint(cursor):
    count, digest = 0, 0
    for row in rows(cursor):
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()
        # An order-independent checksum avoids SQLite/PostgreSQL collation differences.
        digest = (digest + int.from_bytes(hashlib.sha256(encoded).digest())) % (1 << 256)
        count += 1
    return count, str(digest)


def initialize(service, database):
    if service == "agent":
        from src.agent.chat_service import ChatService
        ChatService(None, database)
    elif service == "profiles":
        from src.profiles.store import ProfileStore
        ProfileStore(database)
    else:
        from src.telegram.delivery_store import DeliveryStore
        DeliveryStore(database)


def migrate(service, source, database):
    initialize(service, database)
    path = Path(source).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as legacy, database.connect() as db:
        legacy.execute("BEGIN")
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended('sqlite-import', 0))")
        db.execute("CREATE TABLE IF NOT EXISTS legacy_imports (service TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")
        queries = {}
        signatures = {}
        for table, columns in TABLES[service].items():
            query = sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table),
            )
            queries[table] = query
            signatures[table] = fingerprint(legacy.execute(query.as_string()))
        signature = json.dumps(signatures, sort_keys=True)
        previous = db.execute("SELECT fingerprint FROM legacy_imports WHERE service = %s", (service,)).fetchone()
        if previous:
            if previous[0] != signature:
                raise ValueError("A different source was already imported. Destination was not changed.")
            return "already imported"
        for table in queries:
            existing = db.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
            if table == "polling":
                existing = 0 if db.execute('SELECT id, "offset" FROM polling').fetchall() == [(1, 0)] else existing
            if existing:
                raise ValueError("Destination contains application data. Import refused without changing it.")
        for table, columns in TABLES[service].items():
            if table == "polling":
                db.execute("DELETE FROM polling")
            insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            )
            cursor = legacy.execute(queries[table].as_string())
            while batch := cursor.fetchmany(500):
                with db.cursor() as target:
                    target.executemany(insert, batch)
            if fingerprint(db.execute(queries[table])) != signatures[table]:
                raise ValueError("Import verification failed. Transaction was rolled back.")
        db.execute("INSERT INTO legacy_imports VALUES (%s, %s)", (service, signature))
    return "imported and verified"


def main():
    parser = argparse.ArgumentParser(description="Import a stopped application's SQLite snapshot into an empty PostgreSQL database. Sources are read-only.")
    parser.add_argument("service", choices=TABLES)
    parser.add_argument("source")
    args = parser.parse_args()
    try:
        result = migrate(args.service, args.source, Database.from_env())
    except Exception as error:
        parser.exit(1, f"Import failed ({type(error).__name__}). Check source schema, empty destination, and database configuration; source data was not modified.\n")
    print(f"SQLite migration: {result}.")


if __name__ == "__main__":
    main()
