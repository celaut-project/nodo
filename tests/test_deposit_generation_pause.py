"""A cold-wallet sweep must not run while an incoming deposit is in flight.

``ergo.manager`` sweeps by SPENDING the wallet's boxes, while
``payment_process_validator`` proves a payment by finding an *unspent* box carrying
the deposit token in R4 -- so sweeping that box away rejects a client's honest
payment.

The pause meant to prevent it never worked: ``__manage_interfaces`` assigned
``deposit_generation_locked`` with no ``global``, so it only ever set a
function-local and ``generate_deposit_token`` kept issuing tokens throughout the
sweep. Adding ``global`` alone would have been worse than the bug: the drain loop
waited unbounded for ``pending == 0`` and nothing expires an unpaid token, so the
first abandoned deposit would have locked out every future one for the lifetime of
the process.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system import payment_process
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    payment_process = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DepositGenerationPauseTests(unittest.TestCase):
    def setUp(self):
        payment_process.deposit_generation_locked = False
        self.addCleanup(setattr, payment_process, "deposit_generation_locked", False)

    def test_no_token_is_issued_while_a_sweep_holds_the_pause(self):
        payment_process.deposit_generation_locked = True
        with mock.patch.object(payment_process.env_manager, "get", return_value=True), \
                mock.patch.object(payment_process, "sc") as sc:
            with self.assertRaises(Exception):
                payment_process.generate_deposit_token(client_id="c1")
        sc.add_deposit_token.assert_not_called()

    def test_no_token_is_issued_when_the_operator_closed_new_deposits(self):
        # client.ACCEPT_NEW_DEPOSITS: false -- existing balances still spend, but
        # nobody acquires more MU.
        with mock.patch.object(payment_process.env_manager, "get", return_value=False), \
                mock.patch.object(payment_process, "sc") as sc:
            with self.assertRaises(Exception):
                payment_process.generate_deposit_token(client_id="c1")
        sc.add_deposit_token.assert_not_called()

    def test_the_drain_clears_once_the_pending_tokens_settle(self):
        polls = [[{"id": "t1"}], []]
        with mock.patch.object(payment_process, "sc") as sc, \
                mock.patch.object(payment_process, "sleep"):
            sc.get_deposit_tokens.side_effect = lambda status: polls.pop(0)
            self.assertTrue(payment_process._pause_and_drain_deposits(timeout=60))
        # Held paused for the wait, or a busy node would never reach zero pending.
        self.assertTrue(payment_process.deposit_generation_locked)

    def test_an_unpaid_token_times_out_instead_of_waiting_forever(self):
        with mock.patch.object(payment_process, "sc") as sc, \
                mock.patch.object(payment_process, "sleep"):
            sc.get_deposit_tokens.return_value = [{"id": "never-paid"}]
            self.assertFalse(payment_process._pause_and_drain_deposits(timeout=0))

    def test_the_drain_writes_off_tokens_past_the_ttl(self):
        with mock.patch.object(payment_process, "sc") as sc, \
                mock.patch.object(payment_process, "sleep"):
            sc.expire_pending_deposit_tokens.return_value = 2
            sc.get_deposit_tokens.return_value = []
            self.assertTrue(payment_process._pause_and_drain_deposits(timeout=60))
        sc.expire_pending_deposit_tokens.assert_called_once_with(payment_process.DEPOSIT_TOKEN_TTL)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DepositTokenExpiryTests(unittest.TestCase):
    """Drives the real ``expire_pending_deposit_tokens`` against real SQLite, on the
    real schema: the date arithmetic and the NULL case are what can break, and a mock
    would assert neither."""

    def setUp(self):
        import sqlite3
        from src.database import migrate
        from src.database.sql_connection import SQLConnection

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        migrate.create_tables(self.conn.cursor())

        self.sc = SQLConnection.__new__(SQLConnection)  # no storage dirs, no real file
        previous, SQLConnection._connection = SQLConnection._connection, self.conn
        self.addCleanup(setattr, SQLConnection, "_connection", previous)
        self.addCleanup(self.conn.close)

    def _insert(self, token_id: str, status: str, created_at: str):
        self.conn.execute(
            f"INSERT INTO deposit_tokens (id, client_id, status, created_at) "
            f"VALUES (?, 'c1', ?, {created_at})",
            (token_id, status),
        )

    def _status(self, token_id: str) -> str:
        return self.conn.execute(
            "SELECT status FROM deposit_tokens WHERE id = ?", (token_id,)
        ).fetchone()["status"]

    def test_a_fresh_token_survives(self):
        self._insert("fresh", "pending", "CURRENT_TIMESTAMP")
        self.assertEqual(self.sc.expire_pending_deposit_tokens(3600), 0)
        self.assertEqual(self._status("fresh"), "pending")

    def test_a_token_older_than_the_ttl_is_written_off(self):
        self._insert("stale", "pending", "datetime('now', '-2 hours')")
        self.assertEqual(self.sc.expire_pending_deposit_tokens(3600), 1)
        self.assertEqual(self._status("stale"), "rejected")

    def test_a_settled_token_is_never_touched(self):
        self._insert("paid", "payed", "datetime('now', '-9 hours')")
        self.assertEqual(self.sc.expire_pending_deposit_tokens(3600), 0)
        self.assertEqual(self._status("paid"), "payed")

    def test_a_token_add_deposit_token_just_issued_is_not_expirable(self):
        # `add_deposit_token` names no created_at, so this is what proves the column
        # default stamps one. Were it ever NULL, every token would be born expired.
        token = self.sc.add_deposit_token(client_id="c1", status="pending")
        self.assertEqual(self.sc.expire_pending_deposit_tokens(3600), 0)
        self.assertEqual(self._status(token), "pending")


if __name__ == "__main__":
    unittest.main()
