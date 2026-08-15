"""Every payment leaves a local row saying who and how much.

The ledger cannot answer "what did we send that peer": an address is not a peer id,
and a deposit token is not a client id. Until this table existed the answer lived in
a log line and a counter (`add_balance_to_peer`), so it did not survive a restart.

The case worth guarding is the failed one. A payment is two steps -- broadcast the
transaction, then tell the peer about it -- and when the second fails the money is
already gone. That is the row an operator goes looking for, and the easiest one to
forget to write.
"""
import unittest
from contextlib import contextmanager
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.payment_system import payment_process
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    payment_process = None  # type: ignore[assignment]

CONTRACT_HASH = "1c691f72aad8533f1e0815cb6dd9f302637d5c60824c8a92684fe50cdd4b82bd"
SCRIPT = bytes.fromhex("0008cd03" + "77" * 32)
TX_ID = "9d0f1c2b3a4e5d6c7b8a99887766554433221100ffeeddccbbaa998877665544"


class _Envs:
    """Payment-envs registry whose contract reports a transaction id, as Ergo's does."""

    DEMOS = ()

    def __init__(self, submitted_tx_id=TX_ID):
        self._submitted_tx_id = submitted_tx_id
        self._reporter = None

    def available_payment_process(self):
        def process_payment(amount, deposit_token, ledger, script):
            if self._reporter and self._submitted_tx_id:
                self._reporter(self._submitted_tx_id)
            return celaut_pb2.Contract(ledger=ledger)

        return {CONTRACT_HASH: process_payment}

    def check_sender_balances(self):
        return {CONTRACT_HASH: lambda amount: True}

    @contextmanager
    def transaction_id_reporting(self, reporter):
        self._reporter = reporter
        try:
            yield
        finally:
            self._reporter = None


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OutgoingPaymentRecordTests(unittest.TestCase):

    def _pay(self, communicated: bool, envs=None):
        ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo chain", formal=b"")
        connection = mock.MagicMock()
        peer_payment_process = getattr(payment_process, "__peer_payment_process")

        with mock.patch.object(payment_process, "_payment_envs",
                               return_value=envs or _Envs()), \
                mock.patch.object(payment_process, "sc", connection), \
                mock.patch.object(payment_process, "get_peer_contract_instances",
                                  return_value=iter([(SCRIPT, ledger)])), \
                mock.patch.object(payment_process, "ledger_balancer",
                                  side_effect=lambda ledger_generator: ledger_generator), \
                mock.patch.object(payment_process, "_reputation_interface"), \
                mock.patch.object(payment_process, "__obtain_deposit_token",
                                  return_value="deposit-token-1", create=True), \
                mock.patch.object(payment_process, "__attempt_payment_communication",
                                  return_value=communicated, create=True):
            paid = peer_payment_process(peer_id="peer-1", amount=1000)

        connection.record_payment.assert_called_once()
        return paid, connection.record_payment.call_args.kwargs

    def test_an_acknowledged_payment_records_the_peer_the_amount_and_the_transaction(self):
        paid, row = self._pay(communicated=True)

        self.assertTrue(paid)
        self.assertEqual(row["direction"], "out")
        self.assertEqual(row["status"], "communicated")
        self.assertEqual(row["peer_id"], "peer-1")
        self.assertEqual(row["amount_mu"], 1000)
        self.assertEqual(row["tx_id"], TX_ID)
        self.assertEqual(row["deposit_token"], "deposit-token-1")
        self.assertEqual(row["ledger"], "ergo")
        self.assertEqual(row["contract_hash"], CONTRACT_HASH)
        # The same form `contract_instance.address` stores, so the two can be joined.
        self.assertEqual(row["address"], SCRIPT.hex())

    def test_a_broadcast_the_peer_never_acknowledged_is_still_recorded(self):
        """Money left, no balance arrived. Losing this row loses the incident."""
        paid, row = self._pay(communicated=False)

        self.assertFalse(paid)
        self.assertEqual(row["status"], "unacknowledged")
        self.assertEqual(row["tx_id"], TX_ID)
        self.assertEqual(row["peer_id"], "peer-1")

    def test_a_contract_that_reports_no_transaction_records_the_payment_anyway(self):
        """The simulated contract has no chain and no id. The payment still happened."""
        _, row = self._pay(communicated=True, envs=_Envs(submitted_tx_id=None))

        self.assertIsNone(row["tx_id"])
        self.assertEqual(row["status"], "communicated")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class IncomingPaymentRecordTests(unittest.TestCase):

    def _receive(self, valid: bool):
        ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo chain", formal=b"")
        connection = mock.MagicMock()
        connection.deposit_token_exists.return_value = True
        connection.client_id_from_deposit_token.return_value = "client-1"
        connection.get_deposit_tokens.return_value = []
        manager = mock.MagicMock()
        manager.increase_local_balance_for_client.return_value = True

        with mock.patch.object(payment_process, "sc", connection), \
                mock.patch.object(payment_process, "_manager_module", return_value=manager), \
                mock.patch.object(payment_process, "__check_payment_process",
                                  return_value=valid, create=True):
            credited = payment_process.validate_payment_process(
                amount=1000, ledger=ledger, contract=b"contract",
                script=SCRIPT, token="deposit-token-1",
            )

        connection.record_payment.assert_called_once()
        return credited, connection.record_payment.call_args.kwargs

    def test_a_validated_deposit_records_the_client_that_paid(self):
        credited, row = self._receive(valid=True)

        self.assertTrue(credited)
        self.assertEqual(row["direction"], "in")
        self.assertEqual(row["status"], "accepted")
        self.assertEqual(row["client_id"], "client-1")
        self.assertEqual(row["deposit_token"], "deposit-token-1")
        self.assertEqual(row["amount_mu"], 1000)

    def test_a_refused_deposit_is_recorded_too(self):
        """The deposit a client will ask about is the one that was not credited."""
        credited, row = self._receive(valid=False)

        self.assertFalse(credited)
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["client_id"], "client-1")


if __name__ == "__main__":
    unittest.main()
