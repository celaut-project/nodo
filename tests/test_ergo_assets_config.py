"""The `ASSETS` block: what is refused at startup, and what is only warned about.

The line between the two is whether the operator could have meant it. A token id that
is not 64 hex characters, or two assets claiming one display unit, cannot be meant --
and a node that started with either would advertise a payment method it cannot honour,
or render every figure in the wrong money. A rate on an odd scale might be meant, so it
is a warning: an operator mid-edit must not be locked out of their own config.

The rules are the contract's own (`contracts/ergo/rate.parse_assets`) and are called
from validation rather than restated there. A rule enforced at startup but not when the
list is read gives a config the node boots on and then refuses to settle through.
"""
import unittest

IMPORT_ERROR = None
try:
    from src.utils.config_validation import (
        ConfigValidationError,
        validate_ergo_config,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

TOKEN = "ab" * 32
OTHER = "cd" * 32


def _asset(**overrides):
    entry = {
        "TOKEN_ID": TOKEN,
        "SYMBOL": "SigUSD",
        "UNIT_NAME": "sigusd",
        "DECIMALS": 2,
        "MU_PER_UNIT": 20_000_000,
    }
    entry.update(overrides)
    return entry


def _config(assets, *, mu_per_nanoerg=1, units=None):
    return {
        "identity": {"MNEMONIC": "one two three"},
        "pricing": {"RAM_MU_PER_GIB_HOUR": 1_000_000},
        "ui": {"UNITS": units or {}},
        "ledgers": {
            "ergo": {
                "WALLET_MNEMONIC": "one two three",
                "payments": {
                    "MU_PER_NANOERG": mu_per_nanoerg,
                    "HOT_WALLET_LIMITS": "100",
                    "COLD_WALLET": "",
                    "COLD_WALLET_MIN_TRANSFER": "1",
                    "ASSETS": assets,
                },
            }
        },
    }


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ErgoAssetConfigTests(unittest.TestCase):
    def _validate(self, assets, **kwargs):
        warnings = []
        validate_ergo_config(
            _config(assets, **kwargs), reputation_enabled=False,
            warn=warnings.append,
        )
        return warnings

    def test_no_assets_is_valid_and_silent(self):
        # The default. A node that has not opted in behaves exactly as before.
        self.assertEqual(self._validate([]), [])

    def test_a_well_formed_asset_is_accepted_silently(self):
        # 2e7 MU per cent and 1 MU per nanoERG say one SigUSD is 2 ERG. Plausible.
        self.assertEqual(self._validate([_asset()]), [])

    def test_a_token_id_that_is_not_64_hex_is_refused(self):
        for bad in ("", "SigUSD", "ab" * 31, "zz" * 32):
            with self.subTest(token_id=bad):
                with self.assertRaisesRegex(ConfigValidationError, "64-character hex"):
                    self._validate([_asset(TOKEN_ID=bad)])

    def test_two_assets_sharing_a_display_unit_are_refused(self):
        with self.assertRaisesRegex(ConfigValidationError, "UNIT_NAME"):
            self._validate([_asset(), _asset(TOKEN_ID=OTHER, SYMBOL="X")])

    def test_an_asset_taking_the_ledgers_own_unit_name_is_refused(self):
        with self.assertRaisesRegex(ConfigValidationError, "native unit"):
            self._validate([_asset(UNIT_NAME="erg")])

    def test_an_asset_clashing_with_a_hand_declared_unit_is_refused(self):
        # `monetary.display_unit` prefers what a contract contributes, so the
        # operator's own block would be read by nobody rather than clash.
        with self.assertRaisesRegex(ConfigValidationError, "ui.UNITS"):
            self._validate([_asset()], units={"sigusd": {"MU_PER_UNIT": 1}})

    def test_the_same_token_declared_twice_is_refused(self):
        with self.assertRaisesRegex(ConfigValidationError, "two rates"):
            self._validate([_asset(), _asset(UNIT_NAME="other", SYMBOL="Other")])

    def test_a_missing_rate_is_refused(self):
        with self.assertRaises(ConfigValidationError):
            self._validate([_asset(MU_PER_UNIT=None)])

    def test_a_limit_finer_than_the_assets_decimals_is_refused(self):
        # It cannot be expressed in that asset at all, and reading as zero would
        # silently sweep everything.
        with self.assertRaisesRegex(ConfigValidationError, "finer than"):
            self._validate([_asset(HOT_WALLET_LIMITS="0.001")])

    def test_a_per_asset_fee_overhead_must_be_a_share(self):
        with self.assertRaisesRegex(ConfigValidationError, "MAX_FEE_OVERHEAD"):
            self._validate([_asset(MAX_FEE_OVERHEAD=2)])
        with self.assertRaisesRegex(ConfigValidationError, "MAX_FEE_OVERHEAD"):
            self._validate([_asset(MAX_FEE_OVERHEAD=0)])

    def test_a_per_asset_donation_percentage_must_be_a_share(self):
        with self.assertRaisesRegex(ConfigValidationError, "DONATION_PERCENTAGE"):
            self._validate([_asset(DONATION_PERCENTAGE="1.5")])

    def test_a_token_worth_less_than_a_nanoerg_warns(self):
        # Nothing priced in it could settle: the smallest output is one base unit.
        [warning] = self._validate([_asset(MU_PER_UNIT=1, DECIMALS=0)],
                                   mu_per_nanoerg=10**9)
        self.assertIn("less than a single nanoERG", warning)
        self.assertIn("power of ten", warning)

    def test_a_rate_that_says_one_token_is_worth_millions_of_erg_warns(self):
        # The mistake worth naming: MU_PER_UNIT is MU per BASE unit, and an operator who
        # read it as "per whole unit" is out by 10**DECIMALS.
        [warning] = self._validate([_asset(MU_PER_UNIT=10**18)])
        self.assertIn("not a market anyone trades in", warning)
        self.assertIn("per BASE unit", warning)

    def test_an_implausible_rate_is_a_warning_and_not_a_refusal(self):
        # The node still starts: an operator mid-edit must not be locked out.
        self._validate([_asset(MU_PER_UNIT=10**18)])

    def test_the_ledgers_own_keys_are_still_checked_with_assets_present(self):
        config = _config([_asset()])
        del config["ledgers"]["ergo"]["payments"]["COLD_WALLET_MIN_TRANSFER"]
        with self.assertRaises(ConfigValidationError):
            validate_ergo_config(config, reputation_enabled=False)


if __name__ == "__main__":
    unittest.main()
