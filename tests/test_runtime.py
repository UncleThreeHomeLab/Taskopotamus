import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.runtime import private_database, read_secret


class RuntimeTests(unittest.TestCase):
    def test_secret_file_takes_precedence_and_errors_do_not_show_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("synthetic-secret")
            self.assertEqual(read_secret("TOKEN", {"TOKEN_FILE": str(path), "TOKEN": "other"}), "synthetic-secret")
            with self.assertRaises(ValueError):
                read_secret("TOKEN", {"TOKEN_FILE": str(path / "missing")})
            with self.assertRaises(ValueError):
                read_secret("TOKEN", {})

    @unittest.skipIf(os.name == "nt", "POSIX mode enforcement is verified in the Linux test container")
    def test_existing_database_and_directory_permissions_are_restricted(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "state"
            parent.mkdir(mode=0o755)
            database = parent / "state.sqlite3"
            with private_database(database) as db:
                db.execute("CREATE TABLE example (id INTEGER)")
            parent.chmod(0o755)
            database.chmod(0o644)
            with private_database(database):
                pass
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
