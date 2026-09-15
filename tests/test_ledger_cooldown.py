"""The ledger table records a cooldown, not a ledger (issue #82).

It used to hold a serialized ``Contract.Ledger`` keyed by the sha3 of those very
bytes, which meant three things at once:

* Which chains exist was stored data rather than a property of the build, although
  ``payment_system/contracts/registry.py`` is the only thing that decides it and an
  advertisement for a chain this node does not implement can never be settled through.
* Two peers describing the same chain with a different ``prose`` advertised two
  different ledgers, so resolving the tag ``"ergo"`` meant scanning the table and
  parsing every row back into a message.
* The one piece of real state on the table -- the double-spending cooldown -- was
  keyed by that hash while its only caller passes the *tag*, so the UPDATE matched no
  row and no ledger was ever actually paused.

What is left is keyed by the tag, and a chain with no row is simply available.
"""
import sqlite3
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
    from src.payment_system.exceptions import DoubleSpendingAttempt
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LedgerCooldownTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        migrate.ensure_tables(self.connection.cursor(), ("ledger",))

        saved = SQLConnection._connection
        SQLConnection._connection = self.connection
        self.addCleanup(lambda: setattr(SQLConnection, "_connection", saved))
        self.sql = SQLConnection()

    def _retry_time(self, tag):
        row = self.connection.execute(
            "SELECT double_spending_retry_time FROM ledger WHERE tag = ?", (tag,)
        ).fetchone()
        return row and row[0]

    def test_the_table_holds_only_a_tag_and_a_deadline(self):
        columns = [row[1] for row in self.connection.execute("PRAGMA table_info(ledger)")]
        self.assertEqual(columns, ["tag", "double_spending_retry_time"])

    def test_an_unknown_ledger_is_available(self):
        # Absence is the normal state: nothing has gone wrong on this chain.
        self.assertTrue(self.sql.check_if_ledger_is_available(ledger="ergo"))

    def test_a_double_spending_attempt_pauses_that_ledger(self):
        self.sql.update_double_attempt_retry_time_on_ledger(ledger="ergo")
        self.assertFalse(self.sql.check_if_ledger_is_available(ledger="ergo"))

    def test_only_that_ledger_is_paused(self):
        self.sql.update_double_attempt_retry_time_on_ledger(ledger="ergo")
        self.assertTrue(self.sql.check_if_ledger_is_available(ledger="bitcoin"))

    def test_a_second_attempt_extends_the_same_row(self):
        self.sql.update_double_attempt_retry_time_on_ledger(ledger="ergo")
        first = self._retry_time("ergo")
        self.sql.update_double_attempt_retry_time_on_ledger(ledger="ergo")
        self.assertEqual(
            len(self.connection.execute("SELECT tag FROM ledger").fetchall()), 1
        )
        self.assertGreaterEqual(self._retry_time("ergo"), first)

    def test_an_elapsed_cooldown_releases_the_ledger(self):
        self.connection.execute(
            "INSERT INTO ledger (tag, double_spending_retry_time) "
            "VALUES ('ergo', DATETIME('now', '-1 minute'))"
        )
        # The deadline is compared in SQL, against the clock that wrote it. Compared in
        # Python it was read from `'YYYY-MM-DD HH:MM:SS'` against an `isoformat()`
        # string, where the space sorts before the `T` -- so a *live* cooldown read as
        # expired, and `datetime.utcnow()` raised AttributeError before it got that far.
        self.assertTrue(self.sql.check_if_ledger_is_available(ledger="ergo"))

    def test_a_live_cooldown_holds_the_ledger(self):
        self.connection.execute(
            "INSERT INTO ledger (tag, double_spending_retry_time) "
            "VALUES ('ergo', DATETIME('now', '+1 minute'))"
        )
        self.assertFalse(self.sql.check_if_ledger_is_available(ledger="ergo"))

    def test_the_exception_is_what_writes_the_cooldown(self):
        # `DoubleSpendingAttempt(LEDGER)` is raised with the tag; the row it writes has
        # to be the one `check_if_ledger_is_available` reads back.
        DoubleSpendingAttempt("ergo")
        self.assertFalse(self.sql.check_if_ledger_is_available(ledger="ergo"))


if __name__ == "__main__":
    unittest.main()
