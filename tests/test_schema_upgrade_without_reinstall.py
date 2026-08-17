"""A node that pulled new code and restarted must not run against an older schema.

`migrate` only runs from the setup scripts, and `nodo migrate` *deletes* the database
before recreating it -- so restarting the service is the whole upgrade path most
operators will take, and it never creates a table added since they installed.

That is survivable for a feature that simply does nothing. It is not survivable for
reputation: the score and its event are written in one transaction, so a missing
`reputation_events` table would roll the score back too, and a peer that failed us
would keep its old score forever.
"""
import sqlite3
import unittest

IMPORT_ERROR = None
try:
    from src.database.migrate import ensure_tables
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


ADDED_LATER = ("payments", "reputation_events", "service_reputation")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EnsureTablesTests(unittest.TestCase):

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)

    def _tables(self):
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    def test_a_database_from_before_these_tables_gets_them(self):
        ensure_tables(self.connection.cursor(), ADDED_LATER)
        self.assertTrue(set(ADDED_LATER).issubset(self._tables()))

    def test_it_creates_only_what_it_was_asked_for(self):
        """It is not a migration; a node still installs with `migrate`."""
        ensure_tables(self.connection.cursor(), ("payments",))
        self.assertEqual(self._tables(), {"payments", "sqlite_sequence"})

    def test_running_it_twice_changes_nothing(self):
        cursor = self.connection.cursor()
        ensure_tables(cursor, ADDED_LATER)
        cursor.execute(
            "INSERT INTO payments (direction, status, amount_mu) VALUES ('out', 'communicated', '1')"
        )
        self.connection.commit()

        ensure_tables(cursor, ADDED_LATER)

        rows = cursor.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        self.assertEqual(rows, 1, "an existing table must not be recreated or emptied")

    def test_the_indexes_come_with_their_tables(self):
        ensure_tables(self.connection.cursor(), ADDED_LATER)
        indexes = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
            )
        }
        self.assertIn("idx_payments_peer", indexes)
        self.assertIn("idx_reputation_events_subject", indexes)

    def test_an_unknown_name_is_ignored_rather_than_raising(self):
        ensure_tables(self.connection.cursor(), ("no_such_table",))
        self.assertNotIn("no_such_table", self._tables())


if __name__ == "__main__":
    unittest.main()
