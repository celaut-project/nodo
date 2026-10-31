"""`nodo migrate` is additive: it never deletes the database or a row of it (#518)."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.database import migrate as migrate_module
    from src.commands.db_reset import db_reset_command
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MigrateKeepsDataTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = os.path.join(tmp.name, "database.sqlite")
        original = (migrate_module.DATABASE_FILE, migrate_module.STORAGE)
        migrate_module.DATABASE_FILE, migrate_module.STORAGE = self.database, tmp.name
        self.addCleanup(lambda: setattr(migrate_module, "DATABASE_FILE", original[0]))
        self.addCleanup(lambda: setattr(migrate_module, "STORAGE", original[1]))

    def rows_of_every_table(self):
        db = sqlite3.connect(self.database)
        try:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return {t: db.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
        finally:
            db.close()

    def test_rows_survive_and_it_is_idempotent(self):
        migrate_module.migrate()
        db = sqlite3.connect(self.database)
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        seeded = []
        for table in tables:
            columns = db.execute(f"PRAGMA table_info({table})").fetchall()
            if any(c[3] and c[4] is None and not c[5] for c in columns):
                continue  # NOT NULL without a default: not worth hand-filling here.
            try:
                db.execute(f"INSERT INTO {table} DEFAULT VALUES")
                seeded.append(table)
            except sqlite3.Error:
                continue
        db.commit()
        db.close()
        self.assertTrue(seeded)

        before = self.rows_of_every_table()
        migrate_module.migrate()
        migrate_module.migrate()
        self.assertEqual(self.rows_of_every_table(), before)
        self.assertTrue(any(before[t] for t in seeded))

    def test_db_reset_asks_and_deletes(self):
        migrate_module.migrate()
        with mock.patch("src.commands.daemon.is_serving", return_value=False):
            with mock.patch("builtins.input", return_value="no"):
                self.assertFalse(db_reset_command(self.database))
            self.assertTrue(os.path.exists(self.database))
            with mock.patch("builtins.input", return_value="reset"):
                self.assertTrue(db_reset_command(self.database))
            self.assertFalse(os.path.exists(self.database))

    def test_db_reset_removes_leftover_journals(self):
        migrate_module.migrate()
        for suffix in ("-wal", "-shm", "-journal"):
            open(self.database + suffix, "w").close()
        with mock.patch("src.commands.daemon.is_serving", return_value=False):
            self.assertTrue(db_reset_command(self.database, assume_yes=True))
        for suffix in ("", "-wal", "-shm", "-journal"):
            self.assertFalse(os.path.exists(self.database + suffix))

    def test_db_reset_refuses_while_serving(self):
        migrate_module.migrate()
        with mock.patch("src.commands.daemon.is_serving", return_value=True):
            self.assertFalse(db_reset_command(self.database, assume_yes=True))
        self.assertTrue(os.path.exists(self.database))


if __name__ == "__main__":
    unittest.main()


class InstallScriptsKeepDataTests(unittest.TestCase):
    """What `install.sh` runs on an existing node must not delete the database or storage/."""

    def test_no_install_script_removes_them(self):
        import glob
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scripts = [os.path.join(root, "install.sh"), *glob.glob(os.path.join(root, "bash", "*.sh"))]
        removal = re.compile(r"\brm\b[^\n]*(database|sqlite|storage|STORAGE|DATABASE_FILE)")
        for script in scripts:
            with open(script) as f:
                for number, line in enumerate(f, 1):
                    self.assertIsNone(removal.search(line), f"{script}:{number}: {line.strip()}")
        self.assertFalse(os.path.exists(os.path.join(root, "src", "database", "clear.py")))
