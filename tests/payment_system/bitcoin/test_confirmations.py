"""The payer waits for confirmations, and a replaced transaction is not "not yet".

Bitcoin needs no new deposit-token state: the wait already lives on the payer's side,
exactly as it does on Ergo, so the receiver validates something final and answers in one
call. What differs is only how long the wait is -- which is why this contract declares
its own TTL and stays out of Ergo's sweep pause.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.payment_system.contracts.bitcoin import interface as btc
    from src.payment_system.contracts.bitcoin.backend import BackendUnavailable
    from src.utils.bitcoin_units import script_pubkey_from_address
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    btc = None  # type: ignore[assignment]

ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
LEDGER = None if IMPORT_ERROR else celaut_pb2.Contract.Ledger(tags=["bitcoin"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfirmationTests(unittest.TestCase):

    def _pay(self, amount_mu=1_000, statuses=((1,),), min_conf=1, fee_rate=5.0):
        """Run one outgoing payment; ``statuses`` is what gettransaction reports, in turn."""
        chain = mock.Mock()
        chain.send_to.return_value = "tx-abc"
        chain.estimate_fee_rate.return_value = fee_rate
        chain.tx_status.side_effect = [
            {"confirmations": c} for (c,) in [tuple(s) if isinstance(s, tuple) else (s,)
                                              for s in statuses]
        ]
        with mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc.rate, "mu_per_satoshi", return_value=Decimal(1)), \
                mock.patch.object(btc, "NETWORK", lambda: "mainnet"), \
                mock.patch.object(btc, "MIN_CONFIRMATIONS", lambda: min_conf), \
                mock.patch.object(btc, "MAX_FEE_RATE_SAT_VB", lambda: 100.0), \
                mock.patch.object(btc, "WAIT_TX_SLEEP_TIME", 0), \
                mock.patch.object(btc, "WAIT_TX_TIME", len(statuses)), \
                mock.patch.object(btc, "sleep", lambda _: None):
            contract = btc.process_payment(
                amount=amount_mu, deposit_token="deposit-token-1", ledger=LEDGER,
                script=script_pubkey_from_address(ADDRESS),
            )
        return contract, chain

    def test_the_token_is_carried_in_an_op_return(self):
        _, chain = self._pay()
        self.assertEqual(
            chain.send_to.call_args.kwargs["op_return"], b"deposit-token-1"
        )

    def test_it_returns_only_once_the_transaction_is_confirmed_enough(self):
        contract, chain = self._pay(statuses=(0, 0, 2), min_conf=2)
        self.assertEqual(chain.tx_status.call_count, 3)
        self.assertEqual(list(contract.ledger.tags), ["bitcoin"])

    def test_a_replaced_transaction_is_not_treated_as_pending(self):
        """Core reports negative confirmations for a replaced or reorged transaction.

        Waiting longer cannot make it true again, so it has to stop rather than poll
        until the deposit token expires.
        """
        with self.assertRaisesRegex(ValueError, "replaced or left the chain"):
            self._pay(statuses=(0, -1, 3))

    def test_it_gives_up_rather_than_polling_for_ever(self):
        with self.assertRaisesRegex(TimeoutError, "did not reach"):
            self._pay(statuses=(0, 0))

    def test_an_amount_below_the_dust_threshold_is_refused_before_broadcasting(self):
        with self.assertRaisesRegex(ValueError, "dust threshold"):
            self._pay(amount_mu=100)

    def test_a_fee_rate_above_the_ceiling_is_refused_rather_than_paid(self):
        """Clamping would build a transaction the network is not accepting.

        That does not fail -- it sits unconfirmed, holding a deposit token that
        eventually expires. Refusing leaves the money where it is.
        """
        with self.assertRaises(BackendUnavailable):
            self._pay(fee_rate=500.0)

    def test_this_contract_declares_its_own_slower_deadline(self):
        """One confirmation at a low fee rate routinely takes longer than an hour.

        A global TTL sized for Ergo would expire the token first and reject an honest
        payment with the money already on-chain -- the one direction an accounting error
        must never fall in.
        """
        from src.payment_system.contracts.ergo import interface as ergo

        self.assertGreater(btc.DEPOSIT_TOKEN_TTL, ergo.DEPOSIT_TOKEN_TTL)

    def test_a_read_only_backend_declines_to_pay_instead_of_failing_mid_payment(self):
        """The refusal has to reach the payer where it can act on it.

        Funding is the selection: the payer walks the systems it shares with a peer and
        settles through the first it can fund. A backend that holds no key has no
        funding, so it answers no to `check_sender_balance` and the walk moves on --
        nothing is broadcast, and nothing raises halfway through a payment.
        """
        with mock.patch.object(btc, "can_pay", return_value=False):
            self.assertFalse(btc.check_sender_balance(1_000_000))

    def test_this_contract_needs_no_unspent_output_and_no_sweep_pause(self):
        # Its proof is a confirmed transaction, so nothing breaks if the receiving
        # outputs are spent -- and it must not be blocked by a pause bounded far more
        # tightly than its own confirmation time.
        self.assertFalse(btc.needs_unspent_proof)


if __name__ == "__main__":
    unittest.main()
