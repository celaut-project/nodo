"""A client marked unlimited is kept funded; nobody has infinite MU (issue #594).

The maintenance thread tops an unlimited client up to a target with an ordinary credit in
the ledger. Turning the flag off stops the top-ups and takes nothing back. The node's own
dev clients are unlimited by default, and `clients.unmetered`, which nothing ever read, is
gone.
"""

import io
import sqlite3
import unittest
from contextlib import redirect_stdout
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.commands import clients as clients_command_module
    from src.database import ledger
    from src.database.migrate import create_tables, retire_unmetered_column
    from src.database.sql_connection import SQLConnection
    from src.manager import maintain, manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


def _columns(connection, table):
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MigrationTests(unittest.TestCase):
    def _old_database(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE clients (id TEXT PRIMARY KEY, balance_mu TEXT, last_usage FLOAT NULL, "
            "unmetered INTEGER NOT NULL DEFAULT 0)")
        connection.executemany(
            "INSERT INTO clients (id, balance_mu, unmetered) VALUES (?, '5', ?)",
            [("dev-legacy", 0), ("flagged", 1), ("ordinary", 0)])
        return connection

    def _unlimited(self, connection):
        return dict(connection.execute("SELECT id, unlimited FROM clients"))

    def test_the_values_carry_over_and_every_dev_client_is_unlimited(self):
        connection = self._old_database()
        retire_unmetered_column(connection.cursor())
        self.assertEqual(self._unlimited(connection),
                         {"dev-legacy": 1, "flagged": 1, "ordinary": 0})

    def test_the_old_column_is_gone(self):
        connection = self._old_database()
        retire_unmetered_column(connection.cursor())
        self.assertNotIn("unmetered", _columns(connection, "clients"))
        self.assertIn("unlimited", _columns(connection, "clients"))

    def test_the_balances_survive(self):
        connection = self._old_database()
        retire_unmetered_column(connection.cursor())
        self.assertEqual(
            {row[0]: row[1] for row in connection.execute("SELECT id, balance_mu FROM clients")},
            {"dev-legacy": "5", "flagged": "5", "ordinary": "5"})

    def test_it_runs_once_and_is_a_no_op_after(self):
        connection = self._old_database()
        retire_unmetered_column(connection.cursor())
        connection.execute("UPDATE clients SET unlimited = 0 WHERE id = 'dev-legacy'")
        retire_unmetered_column(connection.cursor())
        # The operator's choice for a dev client is not undone the next time it runs.
        self.assertEqual(self._unlimited(connection)["dev-legacy"], 0)

    def test_the_values_are_copied_only_by_the_call_that_adds_the_column(self):
        # What a node whose SQLite cannot drop a column looks like after its first start:
        # both columns there. A later start must not switch back on what the operator
        # turned off.
        connection = self._old_database()
        connection.execute("ALTER TABLE clients ADD COLUMN unlimited INTEGER NOT NULL DEFAULT 0")
        connection.execute("UPDATE clients SET unlimited = 0")
        retire_unmetered_column(connection.cursor())
        self.assertEqual(set(self._unlimited(connection).values()), {0})

    def test_a_failed_drop_does_not_fail_the_migration(self):
        connection = self._old_database()

        class NoDropCursor:
            """A cursor on an SQLite too old to drop a column."""

            def __init__(self, cursor):
                self._cursor = cursor

            def execute(self, sql, *args):
                if "DROP COLUMN" in sql:
                    raise sqlite3.OperationalError('near "DROP": syntax error')
                return self._cursor.execute(sql, *args)

        retire_unmetered_column(NoDropCursor(connection.cursor()))
        self.assertEqual(self._unlimited(connection)["flagged"], 1)
        self.assertIn("unmetered", _columns(connection, "clients"))

    def test_a_fresh_database_and_a_missing_table_are_left_alone(self):
        fresh = sqlite3.connect(":memory:")
        create_tables(fresh.cursor())
        self.assertNotIn("unmetered", _columns(fresh, "clients"))
        self.assertIn("unlimited", _columns(fresh, "clients"))
        retire_unmetered_column(fresh.cursor())
        retire_unmetered_column(sqlite3.connect(":memory:").cursor())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        self._original_connection = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection

    def tearDown(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original_connection


class FlagTests(DatabaseTestCase):
    def test_a_client_is_not_unlimited_unless_marked(self):
        self.sc.add_client("plain", 10, None)
        self.sc.add_client("kept", 10, None, unlimited=True)
        self.assertFalse(self.sc.client_is_unlimited("plain"))
        self.assertTrue(self.sc.client_is_unlimited("kept"))
        self.assertFalse(self.sc.client_is_unlimited("nobody"))

    def test_it_can_be_turned_on_and_off(self):
        self.sc.add_client("a", 10, None)
        self.assertTrue(self.sc.set_client_unlimited("a", True))
        self.assertEqual([c["id"] for c in self.sc.get_unlimited_clients()], ["a"])
        self.assertTrue(self.sc.set_client_unlimited("a", False))
        self.assertEqual(self.sc.get_unlimited_clients(), [])

    def test_a_client_that_does_not_exist_cannot_be_marked(self):
        self.assertFalse(self.sc.set_client_unlimited("nobody", True))

    def test_turning_it_off_takes_nothing_back(self):
        self.sc.add_client("a", 777, None, unlimited=True)
        self.sc.set_client_unlimited("a", False)
        self.assertEqual(self.sc.get_client_balance("a")[0], 777)

    def test_the_dev_clients_the_node_makes_are_unlimited(self):
        client_id = manager._create_dev_client("dev-", amount_mu=0)
        self.assertTrue(self.sc.client_is_unlimited(client_id))


class TopUpTests(DatabaseTestCase):
    def _top_up(self, target):
        with mock.patch.object(manager, "unlimited_target_balance_mu", return_value=target):
            return manager.top_up_unlimited_clients()

    def _balance(self, client_id):
        return self.sc.get_client_balance(client_id)[0]

    def test_it_tops_up_to_the_target_and_no_further(self):
        self.sc.add_client("a", 30, None, unlimited=True)
        self.assertEqual(self._top_up(1000), 1)
        self.assertEqual(self._balance("a"), 1000)
        self.assertEqual(self._top_up(1000), 0)

    def test_it_replaces_what_the_client_spent(self):
        self.sc.add_client("a", 1000, None, unlimited=True)
        self.sc.move_balance("a", -400, ledger.MAINTENANCE)
        self._top_up(1000)
        self.assertEqual(self._balance("a"), 1000)

    def test_a_client_over_the_target_is_not_touched(self):
        self.sc.add_client("a", 5000, None, unlimited=True)
        self.assertEqual(self._top_up(1000), 0)
        self.assertEqual(self._balance("a"), 5000)

    def test_a_client_that_is_not_unlimited_is_never_topped_up(self):
        self.sc.add_client("plain", 1, None)
        self.assertEqual(self._top_up(1000), 0)
        self.assertEqual(self._balance("plain"), 1)

    def test_turning_it_off_stops_the_top_ups_and_keeps_what_it_holds(self):
        self.sc.add_client("a", 10, None, unlimited=True)
        self._top_up(1000)
        self.sc.set_client_unlimited("a", False)
        self.sc.move_balance("a", -600, ledger.MAINTENANCE)
        self._top_up(1000)
        self.assertEqual(self._balance("a"), 400)

    def test_a_target_of_zero_turns_the_top_up_off(self):
        self.sc.add_client("a", 10, None, unlimited=True)
        self.assertEqual(self._top_up(0), 0)
        self.assertEqual(self._balance("a"), 10)

    def test_the_top_up_is_an_ordinary_credit_in_the_ledger(self):
        self.sc.add_client("a", 100, None, unlimited=True)
        self._top_up(1000)
        row = self.sc._execute(
            "SELECT kind, amount_mu, detail FROM balance_movements "
            "WHERE account_id = 'a' ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertEqual(row["kind"], ledger.CREDIT)
        self.assertEqual(int(row["amount_mu"]), 900)
        self.assertIn(manager.UNLIMITED_CLIENT_CREDIT, row["detail"])

    def test_one_that_cannot_be_credited_does_not_stop_the_others(self):
        self.sc.add_client("a", 0, None, unlimited=True)
        self.sc.add_client("b", 0, None, unlimited=True)
        real = self.sc.move_balance
        calls = []

        def flaky(account, *args, **kwargs):
            calls.append(account)
            if account == "a":
                raise RuntimeError("locked")
            return real(account, *args, **kwargs)

        with mock.patch.object(manager.sc, "move_balance", side_effect=flaky):
            self.assertEqual(self._top_up(1000), 1)
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(self._balance("b"), 1000)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TargetConfigTests(unittest.TestCase):
    def test_the_target_comes_from_the_config(self):
        with mock.patch.object(manager.env_manager, "get", return_value=123):
            self.assertEqual(manager.unlimited_target_balance_mu(), 123)

    def test_an_unreadable_target_falls_back_to_the_default(self):
        with mock.patch.object(manager.env_manager, "get", return_value="junk"):
            self.assertEqual(manager.unlimited_target_balance_mu(),
                             manager.DEFAULT_UNLIMITED_TARGET_BALANCE_MU)

    def test_the_default_is_a_finite_figure_a_person_can_read(self):
        self.assertEqual(manager.DEFAULT_UNLIMITED_TARGET_BALANCE_MU, 100_000_000_000)

    def test_the_maintenance_thread_tops_up_with_the_dev_client_check(self):
        with mock.patch.object(maintain, "ensure_dev_client_pools") as pools, \
                mock.patch.object(maintain, "top_up_unlimited_clients") as top_up:
            maintain.check_dev_clients()
        pools.assert_called_once()
        top_up.assert_called_once()


class CommandTests(DatabaseTestCase):
    def _run(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            ok = clients_command_module.clients_command(list(argv))
        return ok, out.getvalue()

    def test_on_marks_the_client_and_says_what_the_node_will_do(self):
        self.sc.add_client("a", 0, None)
        ok, out = self._run("unlimited", "a", "on")
        self.assertTrue(ok)
        self.assertTrue(self.sc.client_is_unlimited("a"))
        self.assertIn("tops it up", out)

    def test_off_says_that_what_it_holds_stays(self):
        self.sc.add_client("a", 50, None, unlimited=True)
        ok, out = self._run("unlimited", "a", "off")
        self.assertTrue(ok)
        self.assertFalse(self.sc.client_is_unlimited("a"))
        self.assertIn("stays with it", out)
        self.assertEqual(self.sc.get_client_balance("a")[0], 50)

    def test_an_unknown_client_is_reported(self):
        ok, out = self._run("unlimited", "nobody", "on")
        self.assertFalse(ok)
        self.assertIn("No client", out)

    def test_anything_but_on_or_off_is_a_usage_error(self):
        self.sc.add_client("a", 0, None)
        for argv in (("unlimited", "a", "maybe"), ("unlimited", "a"), ("unlimited",)):
            ok, out = self._run(*argv)
            self.assertFalse(ok)
            self.assertIn("usage", out)
        self.assertFalse(self.sc.client_is_unlimited("a"))


if __name__ == "__main__":
    unittest.main()
