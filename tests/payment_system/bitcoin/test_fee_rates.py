"""What Core is charged with, and against which transaction (issue #354 §1, §2).

Core charges a fee *rate* against the vsize of the transaction it actually funds. A
fee sized for one shape and then divided by that shape's vsize survives only while
every transaction is that shape -- and none of the three this contract builds are the
same one. A payment carries an `OP_RETURN` and one output, a donation carries one
output per wallet and no `OP_RETURN`, and a sweep spends however many UTXOs the balance
happens to be split across.

The direction the error falls in is what makes it worth pinning: a fee reserved against
a smaller transaction than the one built comes out of the retained hot-wallet balance,
or fails `fundrawtransaction` outright where the balance was sized against the reserve.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.bitcoin import interface as btc
    from src.payment_system.donations.config import Wallet
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    btc = None  # type: ignore[assignment]

WALLET_A = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
WALLET_B = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
RATE = 5.0


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VsizeEstimateTests(unittest.TestCase):
    """The size of a transaction is a function of its shape, and only of its shape."""

    def test_the_payment_estimate_is_the_constant_it_replaced(self):
        # 184 vB, the figure this contract has always reserved a payment's fee against.
        self.assertEqual(btc.vsize_estimate(outputs=1, op_return=True), 184)
        self.assertEqual(btc.VSIZE_ESTIMATE, 184)

    def test_an_op_return_costs_what_the_module_docstring_says(self):
        # ~43 vB, the price of tying a payment to its deposit token.
        self.assertEqual(
            btc.vsize_estimate(outputs=1, op_return=True)
            - btc.vsize_estimate(outputs=1),
            btc.OP_RETURN_VSIZE,
        )

    def test_every_extra_output_makes_the_transaction_larger(self):
        # The finding: a donation split across three wallets is not the transaction a
        # payment's fee was reserved for.
        one = btc.vsize_estimate(outputs=1)
        self.assertEqual(btc.vsize_estimate(outputs=3) - one, 2 * btc.P2WPKH_OUTPUT_VSIZE)

    def test_change_is_counted_because_core_adds_it(self):
        self.assertEqual(
            btc.vsize_estimate(outputs=1, inputs=1),
            btc.TX_OVERHEAD_VSIZE + btc.P2WPKH_INPUT_VSIZE + 2 * btc.P2WPKH_OUTPUT_VSIZE,
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DonationSendTests(unittest.TestCase):
    """`send(outputs, fee_sat)` reads its parameter, and against the right shape."""

    def _send(self, wallets, then=None):
        """Reserve a donation fee for ``wallets``, then run ``then(send, fee)``.

        The `send` closure `_pay_accrued_donations` builds is captured rather than
        reimplemented -- a copy of it here would pass whatever this file says it should
        -- and it is called *inside* the patches, because it reaches `backend()` itself.
        """
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return False

        chain = mock.Mock()
        chain.estimate_fee_rate.return_value = RATE
        with mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "MAX_FEE_RATE_SAT_VB", lambda: 100.0), \
                mock.patch.object(btc, "TARGET_CONF", lambda: 6), \
                mock.patch.object(btc, "SIMULATE_PAYMENTS", lambda: False), \
                mock.patch("src.payment_system.donations.payout.pay_accrued", capture), \
                mock.patch("src.payment_system.donations.config.pay_wallets",
                           return_value=[Wallet(address=a, weight=1) for a in wallets]):
            btc._pay_accrued_donations()
            if then is not None:
                then(captured["send"], captured["fee"])
        return captured, chain

    def test_the_reserved_fee_is_sized_for_every_configured_wallet(self):
        """`can_cover` is asked about `outputs + fee`, so the fee has to be the real one.

        Reserved against a one-output transaction, a three-wallet donation pays more
        than was set aside -- out of the hot balance, or by failing to fund at all.
        """
        captured, _chain = self._send([WALLET_A, WALLET_B])
        self.assertEqual(
            captured["fee"], round(RATE * btc.vsize_estimate(outputs=2))
        )

    def test_the_fee_the_payout_planned_is_the_one_that_is_sent(self):
        """The parameter, not the enclosing reserve.

        `pay_accrued` passes `plan.fee_native` deliberately; the two are equal only for
        as long as `plan_payout` does not adjust a fee, and today it does not. Halved
        here so they differ, which is the only way to tell which one was read.
        """
        planned = {}
        _captured, chain = self._send(
            [WALLET_A, WALLET_B],
            then=lambda send, fee: (
                planned.setdefault("fee", fee // 2),
                send([(WALLET_A, 50_000), (WALLET_B, 50_000)], fee // 2),
            ),
        )
        chain.send_many.assert_called_once()
        sent = chain.send_many.call_args.kwargs["fee_rate_sat_vb"]
        self.assertAlmostEqual(
            sent, planned["fee"] / btc.vsize_estimate(outputs=2)
        )

    def test_the_rate_is_recovered_against_the_shape_being_built(self):
        # Not against VSIZE_ESTIMATE, which describes a payment. The two differ by the
        # OP_RETURN a donation does not carry and by the outputs it does.
        captured, chain = self._send(
            [WALLET_A, WALLET_B],
            then=lambda send, fee: send(
                [(WALLET_A, 50_000), (WALLET_B, 50_000)], fee
            ),
        )
        sent = chain.send_many.call_args.kwargs["fee_rate_sat_vb"]
        self.assertNotAlmostEqual(sent, captured["fee"] / btc.VSIZE_ESTIMATE)
        self.assertAlmostEqual(sent, RATE)

    def test_a_single_wallet_still_goes_through_send_to(self):
        _captured, chain = self._send(
            [WALLET_A],
            then=lambda send, fee: send([(WALLET_A, 50_000)], fee),
        )
        chain.send_to.assert_called_once()
        self.assertAlmostEqual(
            chain.send_to.call_args.kwargs["fee_rate_sat_vb"], RATE
        )

    def test_the_market_rate_is_the_ceiling_whatever_the_planned_fee_says(self):
        """A reserve for more wallets than are paid must not become a higher rate.

        `_fee_rate_sat_vb` refuses to build above `MAX_FEE_RATE_SAT_VB` rather than
        clamping to it, and a rate recovered from a fee is not allowed to walk around
        that refusal from the other side.
        """
        # The whole two-output reserve spent on a transaction paying one wallet.
        _captured, chain = self._send(
            [WALLET_A, WALLET_B],
            then=lambda send, fee: send([(WALLET_A, 50_000)], fee),
        )
        self.assertLessEqual(chain.send_to.call_args.kwargs["fee_rate_sat_vb"], RATE)


if __name__ == "__main__":
    unittest.main()
