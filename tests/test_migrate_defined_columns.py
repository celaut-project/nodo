"""An existing database gets the columns that the definitions in ``migrate.TABLES`` gained.

``CREATE TABLE IF NOT EXISTS`` leaves an existing table as it is. A column that a change
added only to a definition (``launch_on_close``, ``quote_on_close`` and five more, #636
and #642) never reached a database made before that change: after ``nodo migrate``, or
after a pull and a restart, every launch failed with ``no such column: quote_on_close``.
"""

import contextlib
import io
import sqlite3
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()
    from src.database import migrate
    from src.database import sql_connection
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

skip = unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")

#: The columns that #636 and #642 added to the definitions only.
ACTIVITY_WINDOW_AND_PRICE_COLUMNS = {
    "local_instances": [
        "launch_on_close", "launch_closes_at",
        "saved_mem_limit", "saved_cpu_period", "saved_cpu_quota",
    ],
    "delegated_instances": [
        "quote_on_close", "quote_closes_at",
        "quote_price_bounded", "quote_price_volatility", "quote_volatility_period_seconds",
    ],
}


def _quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


def _columns(connection, table):
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _drop(connection, table, gone):
    """Make ``table`` as an older node made it: the same rows, without ``gone``."""
    keep = [c for c in _columns(connection, table) if c not in set(gone)]
    connection.execute(f"CREATE TABLE {table}__old AS SELECT {', '.join(keep)} FROM {table}")
    connection.execute(f"DROP TABLE {table}")
    connection.execute(f"ALTER TABLE {table}__old RENAME TO {table}")


def _fresh():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    _quiet(migrate.create_tables, connection.cursor())
    connection.commit()
    return connection


@skip
class DefinedColumnsTests(unittest.TestCase):
    def test_what_sqlite_can_add_and_what_it_cannot(self):
        columns = migrate.defined_columns(
            "CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY, a TEXT DEFAULT NULL, "
            "b INTEGER NOT NULL DEFAULT 0, c REAL, d TEXT NOT NULL, e TEXT NOT NULL DEFAULT 'x')")
        self.assertEqual(columns, {
            "a": "TEXT DEFAULT NULL",
            "b": "INTEGER NOT NULL DEFAULT 0",
            "c": "REAL DEFAULT NULL",
            "e": "TEXT NOT NULL DEFAULT 'x'",
        })

    def test_every_table_made_before_its_columns_gets_them(self):
        """Whatever change adds a column to a definition, an older table gets it."""
        expected = _fresh()
        connection = _fresh()
        for table in migrate.TABLES:
            with self.subTest(table=table):
                addable = migrate.defined_columns(migrate.TABLES[table])
                _drop(connection, table, addable)
                self.assertFalse(set(addable) & set(_columns(connection, table)))
        _quiet(migrate.create_tables, connection.cursor())
        for table in migrate.TABLES:
            with self.subTest(table=table):
                self.assertEqual(sorted(_columns(connection, table)),
                                 sorted(_columns(expected, table)))

    def test_the_indexes_and_triggers_on_a_column_that_was_added_exist(self):
        connection = _fresh()
        _drop(connection, "local_instances", ["killed_at", "death_reason"])
        _quiet(migrate.create_tables, connection.cursor())
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name = 'local_instances'")}
        self.assertIn("trg_local_instances_no_revive", names)
        # Partial indexes on `killed_at IS NULL`: they fail while the column is missing.
        self.assertIn("idx_local_instances_name_alive", names)
        self.assertIn("idx_local_instances_father_alive", names)

    def test_a_fresh_database_is_not_changed(self):
        before = {t: _columns(_fresh(), t) for t in migrate.TABLES}
        connection = _fresh()
        _quiet(migrate.create_tables, connection.cursor())
        self.assertEqual({t: _columns(connection, t) for t in migrate.TABLES}, before)


@skip
class ActivityWindowColumnsTests(unittest.TestCase):
    """#642: a database from before it, then `nodo migrate`, then a launch."""

    def setUp(self):
        self.connection = _fresh()
        for table, gone in ACTIVITY_WINDOW_AND_PRICE_COLUMNS.items():
            _drop(self.connection, table, gone)
        original = SQLConnection._connection
        SQLConnection._connection = self.connection
        self.addCleanup(setattr, SQLConnection, "_connection", original)

    def _launch(self):
        SQLConnection().add_local_instance(
            father_id="father", container_ip="192.168.200.7", container_id="a",
            name="web", balance_mu=100, serialized_instance="x", service_id="svc",
            virtualizer="ch", mem_limit=1000, disk_space=500, cpu_period=100,
            cpu_quota=50, envs="{}", launch_on_close="stop", launch_closes_at=1234)

    def test_the_old_table_has_no_such_column(self):
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.connection.execute("SELECT launch_on_close FROM local_instances")
        self.assertIn("no such column", str(caught.exception))

    def test_nodo_migrate_adds_them_and_a_launch_works(self):
        _quiet(migrate.create_tables, self.connection.cursor())
        for table, columns in ACTIVITY_WINDOW_AND_PRICE_COLUMNS.items():
            self.assertTrue(set(columns) <= set(_columns(self.connection, table)), table)
        self._launch()
        self.assertEqual(SQLConnection().get_local_closing_terms(), {"a": ("stop", 1234)})

    def test_a_restart_with_no_migrate_adds_them_too(self):
        """A pull and a restart is the usual upgrade: the start of the node adds them."""
        _quiet(sql_connection._ensure_traceability_tables, self.connection)
        for table, columns in ACTIVITY_WINDOW_AND_PRICE_COLUMNS.items():
            self.assertTrue(set(columns) <= set(_columns(self.connection, table)), table)
        self._launch()


@skip
class OneWayMigrationOrderTests(unittest.TestCase):
    def test_unmetered_clients_still_become_unlimited(self):
        """`retire_unmetered_column` copies only when it adds `unlimited`: nothing adds it first."""
        connection = _fresh()
        _drop(connection, "clients", ["unlimited"])
        connection.execute("ALTER TABLE clients ADD COLUMN unmetered INTEGER NOT NULL DEFAULT 0")
        cols = _columns(connection, "clients")
        for client_id, unmetered in (("dev-1", 0), ("paid", 1), ("plain", 0)):
            values = {c: None for c in cols}
            values.update({"id": client_id, "unmetered": unmetered})
            if "balance_mu" in values:
                values["balance_mu"] = "0"
            connection.execute(
                f"INSERT INTO clients ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                list(values.values()))
        connection.commit()
        for entry in (migrate.create_tables, None):
            with self.subTest(path="nodo migrate" if entry else "start of the node"):
                db = sqlite3.connect(":memory:")
                connection.backup(db)
                if entry:
                    _quiet(entry, db.cursor())
                else:
                    _quiet(sql_connection._ensure_traceability_tables, db)
                rows = dict(db.execute("SELECT id, unlimited FROM clients"))
                self.assertEqual(rows, {"dev-1": 1, "paid": 1, "plain": 0})
                self.assertNotIn("unmetered", _columns(db, "clients"))


if __name__ == "__main__":
    unittest.main()
