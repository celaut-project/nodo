"""The cold sweep on Bitcoin: the shared rule, this chain's numbers.

`compute_sweep_amount` is shared rather than copied, and this is what that buys: the
retained-balance arithmetic is verified once, and the only thing Bitcoin brings is its
own dust threshold and a fee that moves with the market. A second copy would be a second
place for it to drift, on the one path where drifting means sending money somewhere it
cannot come back from.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.bitcoin import interface as btc
    from src.payment_system.sweeps import compute_sweep_amount
    from src.utils.bitcoin_units import P2WPKH_DUST_SAT
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    btc = None  # type: ignore[assignment]

COLD = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
FEE = 920  # 184 vB at 5 sat/vB


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SweepArithmeticTests(unittest.TestCase):
    """Integer satoshi throughout, and the retained amounts really are retained."""

    def test_the_excess_is_what_moves(self):
        self.assertEqual(
            compute_sweep_amount(balance=10_000_000, hot_limit=5_000_000,
                                 min_transfer=1_000_000, fee=FEE,
                                 technical_min=P2WPKH_DUST_SAT),
            10_000_000 - 5_000_000 - FEE,
        )

    def test_nothing_moves_below_the_configured_minimum_transfer(self):
        self.assertIsNone(
            compute_sweep_amount(balance=5_500_000, hot_limit=5_000_000,
                                 min_transfer=1_000_000, fee=FEE,
                                 technical_min=P2WPKH_DUST_SAT)
        )

    def test_nothing_moves_below_bitcoins_dust_threshold(self):
        # A minimum transfer of zero still cannot build an output the network refuses.
        self.assertIsNone(
            compute_sweep_amount(balance=5_000_000 + FEE + 100, hot_limit=5_000_000,
                                 min_transfer=0, fee=FEE,
                                 technical_min=P2WPKH_DUST_SAT)
        )
        self.assertEqual(
            compute_sweep_amount(balance=5_000_000 + FEE + P2WPKH_DUST_SAT,
                                 hot_limit=5_000_000, min_transfer=0, fee=FEE,
                                 technical_min=P2WPKH_DUST_SAT),
            P2WPKH_DUST_SAT,
        )

    def test_the_hot_limit_and_the_fee_are_always_retained(self):
        balance, hot = 10_000_000, 5_000_000
        swept = compute_sweep_amount(balance=balance, hot_limit=hot,
                                     min_transfer=0, fee=FEE,
                                     technical_min=P2WPKH_DUST_SAT)
        self.assertEqual(balance - swept - FEE, hot)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SweepTests(unittest.TestCase):

    def _sweep(self, *, balance, cold=COLD, hot="0.05", minimum="0.01", simulate=False,
               network="mainnet"):
        chain = mock.Mock()
        chain.get_balance.return_value = balance
        chain.estimate_fee_rate.return_value = 5.0
        chain.send_to.return_value = "tx-sweep"
        values = {
            "ledgers.bitcoin.payments.HOT_WALLET_LIMITS": hot,
            "ledgers.bitcoin.payments.COLD_WALLET_MIN_TRANSFER": minimum,
        }
        real_get = btc.env_manager.get
        with mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "COLD_WALLET", lambda: cold), \
                mock.patch.object(btc, "NETWORK", lambda: network), \
                mock.patch.object(btc, "MIN_CONFIRMATIONS", lambda: 1), \
                mock.patch.object(btc, "MAX_FEE_RATE_SAT_VB", lambda: 100.0), \
                mock.patch.object(btc, "SIMULATE_PAYMENTS", lambda: simulate), \
                mock.patch.object(
                    btc.env_manager, "get",
                    side_effect=lambda key, default=None: values.get(key, real_get(key, default))):
            btc._sweep_to_cold_wallet()
        return chain

    def test_a_wallet_over_its_limit_is_swept(self):
        chain = self._sweep(balance=10_000_000)
        chain.send_to.assert_called_once()
        address, amount = chain.send_to.call_args.args
        self.assertEqual(address, COLD)
        self.assertEqual(amount, 10_000_000 - 5_000_000 - 920)

    def test_no_cold_wallet_means_nothing_is_swept(self):
        self.assertEqual(self._sweep(balance=10_000_000, cold="").send_to.call_count, 0)

    def test_an_address_for_another_network_is_refused_rather_than_swept_to(self):
        """Savings are what a cold wallet holds.

        The config is validated at load, so an address that does not match here means
        the config changed under a running node -- and sweeping to an address nobody on
        this chain can spend is not recoverable.
        """
        chain = self._sweep(balance=10_000_000, network="testnet")
        self.assertEqual(chain.send_to.call_count, 0)

    def test_simulated_payments_broadcast_nothing(self):
        self.assertEqual(
            self._sweep(balance=10_000_000, simulate=True).send_to.call_count, 0
        )

    def test_a_balance_under_the_limit_is_left_alone(self):
        self.assertEqual(self._sweep(balance=1_000_000).send_to.call_count, 0)


if __name__ == "__main__":
    unittest.main()
