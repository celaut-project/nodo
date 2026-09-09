"""One display unit per configured asset, and a clash that refuses to be silent.

A unit name is what `ui.DISPLAY_UNIT` selects and what `format_mu` renders every figure
in the node through. `envs.display_units` merges with `dict.update`, so two assets
sharing a name would not collide -- the second would simply win, and every amount an
operator reads would be off by the ratio between the two, with nothing raising.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.ergo import rate
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    rate = None  # type: ignore[assignment]

TOKEN = "ab" * 32
OTHER = "cd" * 32


def _asset(token_id=TOKEN, symbol="SigUSD", unit="sigusd", decimals=2, mu=20_000_000):
    return {
        "TOKEN_ID": token_id, "SYMBOL": symbol, "UNIT_NAME": unit,
        "DECIMALS": decimals, "MU_PER_UNIT": mu,
    }


class _Config:
    """The two keys this module reads, and nothing else."""

    def __init__(self, assets, mu_per_nanoerg=1):
        self._values = {
            rate.RATE_KEY: mu_per_nanoerg,
            rate.ASSETS_KEY: assets,
        }

    def get(self, key, default=None):
        return self._values.get(key, default)


def _with(assets, mu_per_nanoerg=1):
    return mock.patch.object(rate, "ConfigManager", lambda: _Config(assets, mu_per_nanoerg))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DisplayUnitTests(unittest.TestCase):
    def test_erg_alone_when_nothing_is_configured(self):
        # A node that has not opted in behaves exactly as it did before tokens existed.
        with _with([]):
            self.assertEqual(list(rate.display_units()), ["erg"])

    def test_one_unit_per_asset(self):
        with _with([_asset(), _asset(OTHER, "SigRSV", "sigrsv", 0, 5)]):
            units = rate.display_units()
        self.assertEqual(list(units), ["erg", "sigusd", "sigrsv"])
        # MU per *whole* unit, which is what a person checks against a price they know:
        # 20000000 MU per cent is 2e9 MU per SigUSD.
        self.assertEqual(units["sigusd"]["MU_PER_UNIT"], Decimal(2_000_000_000))
        self.assertEqual(units["sigusd"]["DECIMALS"], 2)
        self.assertEqual(units["sigusd"]["SYMBOL"], "SigUSD")
        # A zero-decimal token's whole unit is its base unit.
        self.assertEqual(units["sigrsv"]["MU_PER_UNIT"], Decimal(5))

    def test_two_assets_sharing_a_unit_name_raise(self):
        with _with([_asset(), _asset(OTHER, "Other", "sigusd")]):
            with self.assertRaisesRegex(ValueError, "UNIT_NAME"):
                rate.display_units()

    def test_an_asset_cannot_take_the_ledgers_own_unit_name(self):
        # "erg" is contributed by this module itself, so an asset claiming it would
        # replace the native unit rather than clash with another asset.
        with _with([_asset(unit="erg")]):
            with self.assertRaisesRegex(ValueError, "native unit"):
                rate.display_units()

    def test_a_rate_that_is_not_a_whole_number_of_mu_per_unit_raises(self):
        # The advertised figure is MU per whole unit and MU are whole. Rounding it
        # would misprice the method by the rounding, silently.
        with _with([_asset(decimals=0, mu=Decimal("0.5"))]):
            with self.assertRaisesRegex(ValueError, "whole number of MU"):
                rate.display_units()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AssetConfigTests(unittest.TestCase):
    """An asset is its id. Everything else about it is presentation or arithmetic."""

    def test_a_token_id_that_is_not_64_hex_is_refused(self):
        for bad in ("", "SigUSD", "ab" * 31, "zz" * 32, TOKEN.upper() + "ff"):
            with self.subTest(token_id=bad), _with([_asset(token_id=bad)]):
                with self.assertRaisesRegex(ValueError, "64-character hex id"):
                    rate.assets()

    def test_an_uppercase_id_is_accepted_and_normalised(self):
        # Explorers render ids in either case; the same token must not become two.
        with _with([_asset(token_id=TOKEN.upper())]):
            self.assertEqual(rate.assets()[0].token_id, TOKEN)

    def test_the_same_id_twice_is_refused(self):
        with _with([_asset(), _asset(symbol="Other", unit="other")]):
            with self.assertRaisesRegex(ValueError, "two rates"):
                rate.assets()

    def test_a_missing_or_zero_rate_is_refused(self):
        for bad in (None, 0, -1):
            with self.subTest(rate=bad), _with([_asset(mu=bad)]):
                with self.assertRaises(ValueError):
                    rate.assets()

    def test_declaration_order_is_kept(self):
        # It is the payer's preference: the payment walk tries one method and falls
        # through to the next, so the order has to be reproducible.
        with _with([_asset(OTHER, "SigRSV", "sigrsv"), _asset()]):
            self.assertEqual([a.symbol for a in rate.assets()], ["SigRSV", "SigUSD"])

    def test_conversions_go_through_the_assets_own_rate(self):
        with _with([_asset()]):
            asset = rate.assets()[0]
        # 20000000 MU per cent.
        self.assertEqual(rate.mu_to_base_units(60_000_000, asset), 3)
        self.assertEqual(rate.base_units_to_mu(3, asset), 60_000_000)
        # A payment truncates -- never claim more than is owed...
        self.assertEqual(rate.mu_to_base_units(59_999_999, asset), 2)
        # ...and a debt keeps the fraction, because it is accrued from many payments
        # and paid once.
        self.assertEqual(
            rate.mu_to_base_units_exact(10_000_000, asset), Decimal("0.5")
        )
        self.assertEqual(rate.base_units_to_str(1234, asset), "12.34")


if __name__ == "__main__":
    unittest.main()
