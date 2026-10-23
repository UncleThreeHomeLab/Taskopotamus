import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from psycopg import sql
import psycopg

from database_support import isolated_database
from src.profiles.instructions import DEFAULT_PROFILE
from src.profiles.store import ProfileStore
from src.storage.database import Database
from src.storage.migrate_sqlite import TABLES, migrate
from src.storage.prepare import SECRET_NAMES, prepare


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_prepare_preserves_secrets_and_creates_separate_private_folders(self):
        data, credentials = self.root / "storage", self.root / "credentials"
        prepare(data, credentials)
        saved = {name: (credentials / name).read_text() for name in SECRET_NAMES}
        prepare(data, credentials)
        self.assertEqual({name: (credentials / name).read_text() for name in SECRET_NAMES}, saved)
        self.assertEqual(len(set(saved.values())), len(SECRET_NAMES))
        self.assertTrue(all(len(value) >= 32 for value in saved.values()))
        self.assertEqual({path.name for path in data.iterdir()}, {"postgres", "objects", "codex", "legacy"})
        if os.name != "nt":
            self.assertEqual(data.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in credentials.iterdir() if path.suffix != ".pub"))

    @unittest.skipIf(os.name == "nt", "Symlinks require Windows privileges")
    def test_prepare_refuses_secret_symlinks_without_changing_the_target(self):
        credentials = self.root / "credentials"
        credentials.mkdir()
        target = self.root / "target"
        target.write_text("synthetic-original")
        (credentials / SECRET_NAMES[0]).symlink_to(target)
        with self.assertRaises(ValueError):
            prepare(self.root / "storage", credentials)
        self.assertEqual(target.read_text(), "synthetic-original")

    def test_container_preparation_limits_secret_access_to_the_service_group(self):
        data, credentials = self.root / "storage", self.root / "credentials"
        prepare(data, credentials)
        with patch("src.storage.prepare.os.chown") as chown:
            prepare(data, credentials, container=True)
        for name in SECRET_NAMES:
            self.assertIn((credentials / name, -1, 10001), [call.args for call in chown.call_args_list])
            if os.name != "nt":
                self.assertEqual((credentials / name).stat().st_mode & 0o777, 0o640)

    def test_database_password_file_is_not_interpreted_as_connection_settings(self):
        path = self.root / "secret"
        value = "synthetic password ' host=evil.example dbname=other"
        path.write_text(value)
        with patch.dict(os.environ, {"DATABASE_NAME": "synthetic-db", "DATABASE_USER": "synthetic-user",
                                    "DATABASE_PASSWORD_FILE": str(path), "DATABASE_HOST": "postgres"}), \
                patch("src.storage.database.psycopg.connect") as connect:
            Database.from_env().connect()
        self.assertEqual(connect.call_args.kwargs["host"], "postgres")
        self.assertEqual(connect.call_args.kwargs["password"], value)

    def test_concurrent_initial_profile_updates_have_only_one_winner(self):
        store = ProfileStore(isolated_database(self))
        ready = threading.Barrier(2)

        def update(value):
            ready.wait(timeout=5)
            try:
                store.update("42", 0, {"persona": value})
                return "saved"
            except ValueError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ["First preference", "Second preference"]))
        self.assertCountEqual(results, ["saved", "conflict"])
        self.assertEqual(store.get("42")["version"], 1)

    def test_bootstrap_creates_roles_that_cannot_access_other_service_databases(self):
        database = isolated_database(self)
        environment = {**os.environ, "PGHOST": database.settings["host"], "POSTGRES_USER": "postgres"}
        for service in ("AGENT", "PROFILE", "TELEGRAM"):
            path = self.root / (service + "_password")
            path.write_text("synthetic-test-password-'\\-only")
            environment[service + "_DATABASE_PASSWORD_FILE"] = str(path)
        result = subprocess.run(["sh", "deploy/postgres/initialize_databases.sh"], env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, "Offline database bootstrap failed.")
        for service in ("agent", "profiles", "telegram"):
            own = "taskopotamus_" + service
            with psycopg.connect(host=database.settings["host"], dbname=own, user=own) as db:
                flags = db.execute("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user").fetchone()
                self.assertEqual(flags, (False, False, False))
            for other in ("agent", "profiles", "telegram"):
                if other != service:
                    with self.assertRaises(psycopg.OperationalError):
                        psycopg.connect(host=database.settings["host"], dbname="taskopotamus_" + other, user=own)

    def snapshot(self, service, records):
        path = self.root / (service + ".sqlite3")
        with closing(sqlite3.connect(path)) as legacy, legacy:
            for table, columns in TABLES[service].items():
                numeric = {"version", "context_window", "id", "offset", "update_id", "next_chunk"}
                if service == "telegram":
                    numeric.add("chat_id")
                declarations = [f'"{column}" {"INTEGER" if column in numeric else "TEXT"}' for column in columns]
                legacy.execute(f'CREATE TABLE "{table}" ({", ".join(declarations)})')
                placeholders = ", ".join("?" for _ in columns)
                legacy.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', records[table])
        return path

    def test_sqlite_import_preserves_all_stores_and_is_idempotent(self):
        snapshots = {
            "agent": {"conversations": [("42", "synthetic-thread")],
                      "thread_context": [("synthetic-thread", "synthetic-model", 80000)],
                      "replies": [("42", "10", "synthetic-hash", "Saved reply"), ("43", "11", "other-hash", None)]},
            "profiles": {"profiles": [("42", 1, json.dumps({"persona": "Saved preference"}))],
                         "profile_versions": [("42", 0, json.dumps(DEFAULT_PROFILE)), ("42", 1, json.dumps({"persona": "Saved preference"}))]},
            "telegram": {"polling": [(1, 9988776655)], "pending": [(6543219876, 9876543210, json.dumps(["Saved chunk"]), 0)]},
        }
        for service, records in snapshots.items():
            with self.subTest(service=service):
                database = isolated_database(self)
                source = self.snapshot(service, records)
                before = source.read_bytes()
                self.assertEqual(migrate(service, source, database), "imported and verified")
                self.assertEqual(migrate(service, source, database), "already imported")
                self.assertEqual(source.read_bytes(), before)
                with database.connect() as db:
                    for table in records:
                        columns = sql.SQL(", ").join(map(sql.Identifier, TABLES[service][table]))
                        actual = db.execute(sql.SQL("SELECT {} FROM {}").format(columns, sql.Identifier(table))).fetchall()
                        self.assertCountEqual(actual, records[table])

    def test_import_refuses_existing_data_and_rolls_back_missing_schema(self):
        database = isolated_database(self)
        store = ProfileStore(database)
        store.update("42", 0, {"persona": "Keep destination"})
        source = self.snapshot("profiles", {"profiles": [], "profile_versions": []})
        with self.assertRaisesRegex(ValueError, "Destination contains"):
            migrate("profiles", source, database)
        self.assertEqual(store.get("42")["persona"], "Keep destination")
        with closing(sqlite3.connect(source)) as legacy, legacy:
            legacy.execute("DROP TABLE profile_versions")
        with self.assertRaises(sqlite3.OperationalError):
            migrate("profiles", source, database)
        self.assertEqual(store.get("42")["persona"], "Keep destination")

    def test_import_failure_after_first_table_rolls_back_all_rows(self):
        database = isolated_database(self)
        source = self.snapshot("agent", {
            "conversations": [("42", "synthetic-thread")],
            "thread_context": [("synthetic-thread", "synthetic-model", "invalid integer")],
            "replies": [],
        })
        before = source.read_bytes()
        with self.assertRaises(psycopg.Error):
            migrate("agent", source, database)
        with database.connect() as db:
            for table in TABLES["agent"]:
                self.assertEqual(db.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))).fetchone()[0], 0)
        self.assertEqual(source.read_bytes(), before)
