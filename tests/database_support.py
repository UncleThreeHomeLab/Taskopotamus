import os
import uuid
from pathlib import Path

import psycopg
from psycopg import sql

from src.storage.database import Database


def isolated_database(test):
    host = os.environ.get("TEST_DATABASE_SOCKET", "")
    if not host.startswith("/tmp/taskopotamus-tests-") or not Path(host, "test-only").is_file():
        raise RuntimeError("Run the offline tests image; tests never connect to a deployed database.")
    name = "test_" + uuid.uuid4().hex
    settings = {"host": host, "user": "postgres", "dbname": "postgres", "sslmode": "disable"}
    with psycopg.connect(**settings, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    def cleanup():
        with psycopg.connect(**settings, autocommit=True) as db:
            db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

    test.addCleanup(cleanup)
    return Database(**{**settings, "dbname": name})
