"""One tick, one sweep transaction, every asset over its own limits.

An Ergo output carries several assets at once. A sweep per asset would pay a fee per
asset for what is one piece of work -- and each of those fees comes out of the hot
wallet, so the cost of holding N assets would grow with N for no reason.

The other half is the ERG a token sweep costs: the fee, and the box the tokens travel
in. A wallet full of tokens and empty of ERG cannot sweep at all, and has to say so
rather than build a transaction the network refuses.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.ergo import interface, rate
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    interface = None  # type: ignore[assignment]

TOKEN = "ab" * 32
OTHER = "cd" * 32
COLD = "9coldWALLET"


def _asset(token_id=TOKEN, symbol="SigUSD", unit="sigusd", decimals=2):
    return rate.Asset(token_id=token_id, symbol=symbol, unit_name=unit,
                      decimals=decimals, mu_per_base_unit=Decimal(1))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TokenSweepTests(unittest.TestCase):
    def setUp(self):
        self.logged = []
        self.sent = []
        patches = (
            mock.patch.object(interface, "LOGGER", self.logged.append),
            mock.patch.object(interface, "COLD_WALLET", lambda: COLD),
            mock.patch.object(interface, "WALLET_MNEMONIC", lambda: "words"),
            mock.patch.object(interface, "__get_sender_addr", return_value=mock.Mock()),
            mock.patch.object(
                interface, "_send_assets",
                side_effect=lambda outputs, fee_nanoerg: (
                    self.sent.append((outputs, fee_nanoerg)) or "tx-1"
                ),
            ),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _sweep(self, nanoergs, tokens, assets=(), limits=None, erg_limit="0",
               erg_min_transfer="0"):
        """Run one tick's sweep against a wallet and a configuration."""
        limits = limits or {}

        def sweep_limits(asset):
            hot, minimum = limits.get(asset.token_id, ("0", "0"))
            return (
                rate.whole_to_base_units(hot, asset, what="hot"),
                rate.whole_to_base_units(minimum, asset, what="min"),
            )

        balance = {"confirmed": {"nanoErgs": nanoergs, "tokens": [
            {"tokenId": token_id, "amount": amount} for token_id, amount in tokens
        ]}}
        with mock.patch.object(interface, "__balance_total", return_value=balance), \
                mock.patch.object(rate, "assets", return_value=tuple(assets)), \
                mock.patch.object(interface, "_asset_sweep_limits",
                                  side_effect=sweep_limits), \
                mock.patch.object(interface, "_hot_wallet_limit_nanoerg",
                                  return_value=int(Decimal(erg_limit) * 10**9)), \
                mock.patch.object(interface, "_cold_wallet_min_transfer_nanoerg",
                                  return_value=int(Decimal(erg_min_transfer) * 10**9)):
            interface._sweep_to_cold_wallet()
        return self.sent

    def _plenty(self):
        return 10 * 10**9  # 10 ERG

    def test_every_asset_over_its_limit_moves_in_one_transaction(self):
        sent = self._sweep(
            self._plenty(),
            [(TOKEN, 5_000), (OTHER, 700)],
            assets=[_asset(), _asset(OTHER, "SigRSV", "sigrsv", decimals=0)],
            limits={TOKEN: ("10", "1"), OTHER: ("100", "1")},
            erg_limit="1",
        )
        self.assertEqual(len(sent), 1, "one tick must be one transaction")
        [(outputs, fee)] = sent
        [(address, value, tokens)] = outputs
        self.assertEqual(address, COLD)
        self.assertEqual(fee, interface.DEFAULT_FEE)
        # 5000 cents held, 1000 retained (10 whole units); 700 held, 100 retained.
        self.assertEqual(sorted(tokens), sorted([(TOKEN, 4_000), (OTHER, 600)]))
        # And the ERG excess rides in the same box.
        self.assertEqual(value, self._plenty() - 10**9 - interface.DEFAULT_FEE)

    def test_an_asset_under_its_limit_stays_in_the_hot_wallet(self):
        sent = self._sweep(
            self._plenty(), [(TOKEN, 500), (OTHER, 700)],
            assets=[_asset(), _asset(OTHER, "SigRSV", "sigrsv", decimals=0)],
            limits={TOKEN: ("10", "1"), OTHER: ("100", "1")},
            erg_limit="1",
        )
        [(outputs, _fee)] = sent
        self.assertEqual(outputs[0][2], [(OTHER, 600)])

    def test_an_excess_below_the_minimum_transfer_is_not_swept(self):
        # Dust: moving it would cost a fee out of proportion to what moves.
        sent = self._sweep(
            self._plenty(), [(TOKEN, 1_050)],
            assets=[_asset()], limits={TOKEN: ("10", "1")}, erg_limit="1",
        )
        [(outputs, _fee)] = sent
        self.assertEqual(outputs[0][2], [], "50 cents is below a 1-unit minimum")

    def test_tokens_alone_still_sweep_and_the_erg_is_only_the_carrier(self):
        # The ERG balance is under its own limit, so no ERG is being swept -- but the
        # tokens need a box, and its value comes out of the hot wallet.
        sent = self._sweep(
            self._plenty(), [(TOKEN, 5_000)],
            assets=[_asset()], limits={TOKEN: ("10", "1")}, erg_limit="100",
        )
        [(outputs, _fee)] = sent
        self.assertEqual(outputs[0][1], interface.SAFE_MIN_BOX_VALUE)
        self.assertEqual(outputs[0][2], [(TOKEN, 4_000)])

    def test_nothing_to_sweep_sends_nothing(self):
        sent = self._sweep(
            10**6, [(TOKEN, 100)],
            assets=[_asset()], limits={TOKEN: ("10", "1")}, erg_limit="100",
        )
        self.assertEqual(sent, [])
        self.assertIn("Nothing to sweep", " ".join(self.logged))

    def test_a_wallet_without_erg_cannot_sweep_its_tokens(self):
        # And says so: "nothing to sweep" would be a different claim, and wrong.
        sent = self._sweep(
            1_000, [(TOKEN, 5_000)],
            assets=[_asset()], limits={TOKEN: ("10", "1")}, erg_limit="100",
        )
        self.assertEqual(sent, [])
        message = " ".join(self.logged)
        self.assertIn("Not sweeping", message)
        self.assertIn("1 asset(s) stay in the hot wallet", message)

    def test_no_cold_wallet_sweeps_nothing_at_all(self):
        with mock.patch.object(interface, "COLD_WALLET", lambda: ""):
            sent = self._sweep(self._plenty(), [(TOKEN, 5_000)], assets=[_asset()],
                               limits={TOKEN: ("10", "1")})
        self.assertEqual(sent, [])

    def test_an_unconfigured_asset_is_never_swept(self):
        # Only what the operator declared. A token that arrived unasked stays where it
        # is: this node has no rate for it, so it cannot even say what it is worth.
        sent = self._sweep(
            self._plenty(), [(OTHER, 10**6)],
            assets=[_asset()], limits={TOKEN: ("0", "0")}, erg_limit="1",
        )
        [(outputs, _fee)] = sent
        self.assertEqual(outputs[0][2], [])

    def test_a_broken_sweep_never_escapes_the_tick(self):
        # It shares the tick with the donation payout, and a chain that will not answer
        # must not stop the node's other periodic work.
        with mock.patch.object(interface, "__balance_total",
                               side_effect=RuntimeError("explorer unreachable")):
            interface._sweep_to_cold_wallet()
        self.assertIn("explorer unreachable", " ".join(self.logged))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TokenSweepArithmeticTests(unittest.TestCase):
    """The shared rule, in token base units."""

    def test_the_fee_is_not_deducted_from_a_token_excess(self):
        from src.payment_system.sweeps import compute_sweep_amount

        # The fee is ERG. Deducting it from a token balance would retain an arbitrary
        # amount of the token for a cost paid in something else.
        self.assertEqual(
            compute_sweep_amount(balance=100, hot_limit=10, min_transfer=1, fee=0,
                                 technical_min=1),
            90,
        )

    def test_a_single_base_unit_is_a_valid_output(self):
        from src.payment_system.sweeps import compute_sweep_amount

        # Ergo's minimum box value is a floor on ERG, not on a token amount.
        self.assertEqual(
            compute_sweep_amount(balance=11, hot_limit=10, min_transfer=1, fee=0,
                                 technical_min=1),
            1,
        )


if __name__ == "__main__":
    unittest.main()
