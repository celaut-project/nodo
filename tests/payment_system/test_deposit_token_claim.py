"""One deposit token is credited once, however many Payable calls carry it (#601).

The gateway serves `Payable` from a thread pool and the validators only look for the
payment on the ledger, they do not spend it. Every concurrent call with the same
token used to see it pending, find the same box and credit the client again.
"""
import sqlite3
import threading
import time
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
    from src.payment_system import payment_process
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    payment_process = None  # type: ignore[assignment]
    SQLConnection = None  # type: ignore[assignment]

SCRIPT = bytes.fromhex("0008cd03" + "77" * 32)
CALLS = 8


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DepositTokenClaimTests(unittest.TestCase):

    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        migrate.ensure_tables(self.connection.cursor(), ("deposit_tokens",))

        self.sql = SQLConnection()
        saved = SQLConnection._connection
        SQLConnection._connection = self.connection
        self.addCleanup(lambda: setattr(SQLConnection, "_connection", saved))
        self.sql.record_payment = mock.MagicMock()

        self.token = self.sql.add_deposit_token(client_id="client-1", status="pending")
        self.credits = []
        self.validations = 0

    def _status(self):
        return self.connection.execute(
            "SELECT status FROM deposit_tokens WHERE id = ?", (self.token,)
        ).fetchone()["status"]

    def _validate(self, valid=True):
        """One Payable call, with a validator that takes long enough to overlap."""
        ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo chain", formal=b"")

        def validator(*args, **kwargs):
            self.validations += 1
            time.sleep(0.05)
            return valid

        def credit(client_id, amount_mu, detail=None):
            self.credits.append((client_id, amount_mu))
            return True

        manager = mock.MagicMock()
        manager.increase_local_balance_for_client.side_effect = credit
        with mock.patch.object(payment_process, "sc", self.sql), \
                mock.patch.object(payment_process, "_manager_module", return_value=manager), \
                mock.patch.object(payment_process, "_donation_accrual"), \
                mock.patch.object(payment_process, "__check_payment_process",
                                  side_effect=validator, create=True):
            return payment_process.validate_payment_process(
                amount=1000, ledger=ledger, contract=b"contract",
                script=SCRIPT, token=self.token,
            )

    def test_concurrent_calls_with_one_token_credit_once(self):
        results = []
        start = threading.Barrier(CALLS)

        def call():
            start.wait()
            try:
                results.append(self._validate())
            except Exception:
                results.append("refused")

        with mock.patch.object(payment_process, "sc", self.sql):
            threads = [threading.Thread(target=call) for _ in range(CALLS)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(self.credits, [("client-1", 1000)])
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self._status(), "payed")

    def test_a_second_call_after_the_first_is_refused(self):
        self.assertTrue(self._validate())
        with self.assertRaises(Exception):
            self._validate()
        self.assertEqual(len(self.credits), 1)

    def test_a_rejected_validation_ends_rejected(self):
        self.assertFalse(self._validate(valid=False))

        self.assertEqual(self._status(), "rejected")
        self.assertEqual(self.credits, [])

    def test_a_validator_that_raises_is_rejected_not_left_claimed(self):
        ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo chain", formal=b"")
        with mock.patch.object(payment_process, "sc", self.sql), \
                mock.patch.object(payment_process, "__check_payment_process",
                                  side_effect=RuntimeError("explorer down"), create=True):
            self.assertFalse(payment_process.validate_payment_process(
                amount=1000, ledger=ledger, contract=b"contract",
                script=SCRIPT, token=self.token,
            ))
        self.assertEqual(self._status(), "rejected")

    def test_update_deposit_token_does_not_move_a_token_that_is_not_pending(self):
        self.sql.update_deposit_token(token_id=self.token, status="payed")
        self.sql.update_deposit_token(token_id=self.token, status="rejected")
        self.assertEqual(self._status(), "payed")

        self.sql.update_deposit_token(token_id=self.token, status="pending")
        self.assertEqual(self._status(), "payed")


if __name__ == "__main__":
    unittest.main()
