"""An outgoing payment survives a daemon that stops between the broadcast and `Payable`.

A payment is two steps: the transaction goes on the network, and once it is confirmed
the peer is told with `Payable`. The wait between them is long, and a daemon that
stopped in it used to lose the payment: the money was on-chain, the peer never
credited it, and nothing local said so (#523). The row is now written at the
broadcast, and `resume_outgoing_payments` finishes what a previous run left.

The other half of the same bug: a confirmation wait that ran out was handled as a
failed payment, so the walk went on to the next ledger and paid a second time against
the same deposit token.
"""
import sqlite3
import threading
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.payment_system import payment_process
    from src.payment_system.contracts import envs
    from src.payment_system.contracts.registry import MethodKey
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    payment_process = None  # type: ignore[assignment]

CONTRACT_HASH = "1c691f72aad8533f1e0815cb6dd9f302637d5c60824c8a92684fe50cdd4b82bd"
SCRIPT = bytes.fromhex("0008cd03" + "77" * 32)
OTHER_SCRIPT = bytes.fromhex("0008cd03" + "88" * 32)
TX_ID = "9d0f1c2b3a4e5d6c7b8a99887766554433221100ffeeddccbbaa998877665544"
KEY = None if IMPORT_ERROR else MethodKey("ergo", CONTRACT_HASH, "ERG")


class _SyncThread:
    """A `Thread` that runs its target on `start`, so a test sees what it did."""

    def __init__(self, target, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _row(status="broadcast", age_seconds=30, peer_amount_mu="2000"):
    return {
        "tx_id": TX_ID, "status": status, "peer_id": "peer-1",
        "deposit_token": "deposit-token-1", "ledger": "ergo",
        "contract_hash": CONTRACT_HASH, "token_id": "ERG", "address": SCRIPT.hex(),
        "amount_mu": "1000", "peer_amount_mu": peer_amount_mu,
        "age_seconds": age_seconds,
    }


class _Envs:
    """One payment method whose contract reports its transaction as a real one does.

    ``process`` stands for the contract: it gets the id reporter and the script it was
    asked to pay, and either returns what `Payable` carries or raises.
    """

    DEMOS = ()

    def __init__(self, process):
        self._process = process
        self.reporter = None
        self.paid_into = []

    def available_payment_process(self):
        def process_payment(amount, deposit_token, ledger, script):
            self.paid_into.append(script)
            return self._process(self.reporter, script)
        return {KEY: process_payment}

    def check_sender_balances(self):
        return {KEY: lambda amount: True}

    def transaction_id_reporting(self, reporter, method=None):
        from contextlib import contextmanager

        @contextmanager
        def reporting():
            self.reporter = reporter
            yield
        return reporting()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OutgoingPaymentStatesTests(unittest.TestCase):

    def _pay(self, process, communicated=True):
        payment_envs = _Envs(process)
        connection = mock.MagicMock()
        with mock.patch.object(payment_process, "_payment_envs", return_value=payment_envs), \
                mock.patch.object(payment_process, "sc", connection), \
                mock.patch.object(payment_process, "get_peer_contract_instances",
                                  return_value=iter([(SCRIPT, "ergo", "ERG"),
                                                     (OTHER_SCRIPT, "ergo", "ERG")])), \
                mock.patch.object(payment_process, "ledger_balancer",
                                  side_effect=lambda ledger_generator: ledger_generator), \
                mock.patch.object(payment_process, "_reputation_interface"), \
                mock.patch.object(payment_process, "__obtain_deposit_token",
                                  return_value="deposit-token-1", create=True), \
                mock.patch.object(payment_process, "__attempt_payment_communication",
                                  return_value=communicated, create=True), \
                mock.patch.object(payment_process, "resume_outgoing_payments") as resume:
            paid = getattr(payment_process, "__peer_payment_process")(
                peer_id="peer-1",
                plans=[payment_process.SettlementPlan(
                    contract_hash=CONTRACT_HASH, ledger_tag="ergo", asset="ERG",
                    amount=1000, peer_amount=2000,
                )],
            )
        self.paid_into = payment_envs.paid_into
        self.resume = resume
        self.recorded = [call.kwargs["status"] for call in connection.record_payment.call_args_list]
        self.statuses = [call.args for call in connection.set_outgoing_payment_status.call_args_list]
        # Released in every case, so a resume can claim it.
        self.assertNotIn(TX_ID, payment_process._outgoing_in_flight)
        return paid

    def test_the_row_is_written_signed_before_the_send(self):
        def process(report, script):
            report(TX_ID, sent=False)
            report(TX_ID, sent=True)
            return celaut_pb2.Contract()

        self.assertTrue(self._pay(process))

        self.assertEqual(self.recorded, ["signed"])
        self.assertEqual(self.statuses, [(TX_ID, "broadcast"), (TX_ID, "confirmed"),
                                         (TX_ID, "communicated")])

    def test_a_send_the_node_refused_is_failed_and_the_next_ledger_is_tried(self):
        from src.payment_system.exceptions import DoubleSpendingAttempt

        def process(report, script):
            if script == SCRIPT:
                report(TX_ID, sent=False)
                # No ledger: with one, the exception writes a retry time to the real DB.
                raise DoubleSpendingAttempt()
            return celaut_pb2.Contract()

        self.assertTrue(self._pay(process))

        self.assertEqual(self.paid_into, [SCRIPT, OTHER_SCRIPT])
        self.assertEqual(self.statuses[0], (TX_ID, "failed"))

    def test_a_send_that_failed_without_saying_is_resumed_not_paid_again(self):
        """An error from the send does not say whether the network took it."""
        def process(report, script):
            report(TX_ID, sent=False)
            raise ConnectionError("the node did not answer")

        self.assertIsNone(self._pay(process))

        self.assertEqual(self.paid_into, [SCRIPT])
        self.assertEqual(self.recorded, ["signed"])
        self.assertEqual(self.statuses, [])
        self.resume.assert_called_once()

    def test_a_confirmation_that_did_not_arrive_is_resumed_not_paid_again(self):
        def process(report, script):
            report(TX_ID, sent=False)
            report(TX_ID, sent=True)
            raise TimeoutError(f"Can't verify the tx {TX_ID}")

        self.assertIsNone(self._pay(process))

        # One transaction, not one per ledger: the second script was never paid.
        self.assertEqual(self.paid_into, [SCRIPT])
        self.assertEqual(self.statuses, [(TX_ID, "broadcast")])
        self.resume.assert_called_once()

    def test_a_contract_that_reports_only_after_the_send_still_gets_its_row(self):
        def process(report, script):
            report(TX_ID)
            return celaut_pb2.Contract()

        self._pay(process)

        self.assertEqual(self.recorded, ["broadcast"])
        self.assertEqual(self.statuses[-1], (TX_ID, "communicated"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResumeTests(unittest.TestCase):

    def _resume(self, rows, awaiter, communicated=True, ttl=3600):
        connection = mock.MagicMock()
        connection.outgoing_payments_to_resume.return_value = rows
        payment_envs = mock.MagicMock()
        payment_envs.payment_awaiters.return_value = {KEY: awaiter}
        payment_envs.deposit_token_ttls.return_value = {KEY: ttl}
        self.told = []

        def communicate(peer_id, peer_amount, deposit_token, contract_ledger):
            self.told.append((peer_id, peer_amount, deposit_token))
            return communicated

        self.reputation = mock.MagicMock()
        with mock.patch.object(payment_process, "sc", connection), \
                mock.patch.object(payment_process, "_payment_envs", return_value=payment_envs), \
                mock.patch.object(payment_process, "Thread", _SyncThread), \
                mock.patch.object(payment_process, "sleep"), \
                mock.patch.object(payment_process, "_reputation_interface",
                                  return_value=self.reputation), \
                mock.patch.object(payment_process, "__attempt_payment_communication",
                                  side_effect=communicate, create=True):
            started = payment_process.resume_outgoing_payments()

        self.statuses = [call.args for call in connection.set_outgoing_payment_status.call_args_list]
        return started

    def test_a_broadcast_payment_is_confirmed_then_communicated(self):
        awaiter = mock.MagicMock(return_value=celaut_pb2.Contract())

        started = self._resume([_row()], awaiter)

        self.assertEqual(started, 1)
        awaiter.assert_called_once_with(tx_id=TX_ID, script=SCRIPT)
        # The same `Payable` the interrupted payment would have sent.
        self.assertEqual(self.told, [("peer-1", 2000, "deposit-token-1")])
        self.assertEqual(self.statuses, [(TX_ID, "confirmed"), (TX_ID, "communicated")])
        self.assertNotIn(TX_ID, payment_process._outgoing_in_flight)

    def test_a_confirmed_payment_goes_straight_to_the_peer(self):
        awaiter = mock.MagicMock(return_value=celaut_pb2.Contract())

        self._resume([_row(status="confirmed")], awaiter)

        self.assertEqual(self.statuses, [(TX_ID, "communicated")])

    def test_a_wait_that_fails_is_retried_within_the_token_lifetime(self):
        awaiter = mock.MagicMock(side_effect=[TimeoutError("not yet"), celaut_pb2.Contract()])

        self._resume([_row()], awaiter)

        self.assertEqual(awaiter.call_count, 2)
        self.assertEqual(self.statuses[-1], (TX_ID, "communicated"))

    def test_past_the_token_lifetime_an_unconfirmed_payment_is_unacknowledged(self):
        awaiter = mock.MagicMock(side_effect=TimeoutError("not yet"))

        self._resume([_row(age_seconds=4000)], awaiter, ttl=3600)

        self.assertEqual(self.told, [])
        self.assertEqual(self.statuses, [(TX_ID, "unacknowledged")])

    def test_a_signed_payment_that_never_showed_up_is_failed(self):
        """Never sent, or refused: no money moved, so it is not 'unacknowledged'."""
        awaiter = mock.MagicMock(side_effect=TimeoutError("not found"))

        self._resume([_row(status="signed", age_seconds=4000)], awaiter, ttl=3600)

        self.assertEqual(self.told, [])
        self.assertEqual(self.statuses, [(TX_ID, "failed")])

    def test_a_signed_payment_that_confirmed_is_communicated(self):
        awaiter = mock.MagicMock(return_value=celaut_pb2.Contract())

        self._resume([_row(status="signed")], awaiter)

        self.assertEqual(self.statuses, [(TX_ID, "confirmed"), (TX_ID, "communicated")])

    def test_a_late_refusal_is_not_held_against_the_peer(self):
        """Past the token's lifetime the delay was ours; the peer may have written it off."""
        awaiter = mock.MagicMock(return_value=celaut_pb2.Contract())

        self._resume([_row(age_seconds=4000)], awaiter, communicated=False, ttl=3600)

        self.assertEqual(self.statuses[-1], (TX_ID, "unacknowledged"))
        self.reputation.update_peer_reputation.assert_not_called()

    def test_a_payment_this_process_is_already_waiting_on_is_left_alone(self):
        awaiter = mock.MagicMock(return_value=celaut_pb2.Contract())
        self.assertTrue(payment_process._claim_outgoing(TX_ID))
        self.addCleanup(payment_process._release_outgoing, TX_ID)

        started = self._resume([_row()], awaiter)

        self.assertEqual(started, 0)
        awaiter.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PaymentRowStatesTests(unittest.TestCase):
    """The real statements, against an in-memory database built by the real migration."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_tables(self.connection.cursor())
        self.connection.commit()
        self.sc = SQLConnection.__new__(SQLConnection)
        for patch in (
            mock.patch.object(SQLConnection, "_connection", self.connection),
            mock.patch.object(SQLConnection, "_lock", threading.Lock()),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.connection.close)

    def _record(self, tx_id, status, direction="out"):
        self.assertTrue(self.sc.record_payment(
            direction=direction, status=status, amount_mu=1000, tx_id=tx_id,
            peer_id="peer-1", deposit_token="deposit-token-1", ledger="ergo",
            contract_hash=CONTRACT_HASH, token_id="ERG", address=SCRIPT.hex(),
            peer_amount_mu=2000,
        ))

    def test_signed_broadcast_and_confirmed_rows_are_the_ones_resumed(self):
        self._record("tx-signed", "signed")
        self._record("tx-broadcast", "broadcast")
        self._record("tx-confirmed", "confirmed")
        self._record("tx-done", "communicated")
        self._record("tx-lost", "unacknowledged")
        self._record("tx-refused", "failed")
        self._record("tx-in", "accepted", direction="in")

        rows = self.sc.outgoing_payments_to_resume()

        self.assertEqual([row["tx_id"] for row in rows],
                         ["tx-signed", "tx-broadcast", "tx-confirmed"])
        self.assertEqual(rows[0]["peer_amount_mu"], "2000")
        self.assertEqual(rows[0]["deposit_token"], "deposit-token-1")
        self.assertGreaterEqual(rows[0]["age_seconds"], 0)

    def test_a_status_moves_on_the_row_written_at_the_broadcast(self):
        self._record(TX_ID, "broadcast")

        self.assertTrue(self.sc.set_outgoing_payment_status(TX_ID, "confirmed"))
        self.assertTrue(self.sc.set_outgoing_payment_status(TX_ID, "communicated"))

        rows = self.connection.execute("SELECT status FROM payments").fetchall()
        self.assertEqual([row["status"] for row in rows], ["communicated"])
        self.assertEqual(self.sc.outgoing_payments_to_resume(), [])

    def test_a_status_for_an_unknown_transaction_moves_nothing(self):
        self.assertFalse(self.sc.set_outgoing_payment_status("tx-unknown", "communicated"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TransactionIdReportingTests(unittest.TestCase):

    def test_the_id_hook_is_the_settling_methods(self):
        """It resolved `contract_hash` -- the `str` type alias -- so no id ever arrived."""
        factory = mock.MagicMock()
        method = mock.MagicMock(transaction_id_reporting=factory)
        reporter = mock.MagicMock()

        with mock.patch.object(envs, "methods", return_value={KEY: method}):
            envs.transaction_id_reporting(reporter, method=KEY)

        factory.assert_called_once_with(reporter)


if __name__ == "__main__":
    unittest.main()
