"""The Bitcoin ledger block: what is refused, what is warned about, and what is silent.

Structural only -- addresses are checked with bech32 and base58check arithmetic, never
by asking a node. That matters more here than it did for Ergo: a cold wallet is where an
operator's savings go, and validating it over RPC would mean a node that cannot reach
`bitcoind` accepts a typo and sweeps to nowhere.
"""
import unittest

IMPORT_ERROR = None
try:
    from src.utils.bitcoin_units import is_valid_bitcoin_address
    from src.utils.config_validation import (
        ConfigValidationError,
        validate_bitcoin_config,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

MAINNET = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
MAINNET_P2SH = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"
TESTNET = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"


def _config(*, network="mainnet", rate="", mu_per_nanoerg=1, **payments):
    block = {
        "MIN_CONFIRMATIONS": 1,
        "TARGET_CONF": 6,
        "MAX_FEE_RATE_SAT_VB": 100,
        "HOT_WALLET_LIMITS": "0.05",
        "COLD_WALLET": "",
        "COLD_WALLET_MIN_TRANSFER": "0.01",
        "MAX_FEE_OVERHEAD": 0.25,
        "DONATION_PERCENTAGE": "0",
        "DONATION_MIN_TRANSFER": "0.0005",
        "DONATION_WALLETS": [],
        "DONATION_CREDIT_WALLETS": [],
        "MU_PER_SATOSHI": rate,
    }
    block.update(payments)
    return {
        "pricing": {"RAM_MU_PER_GIB_HOUR": 1_000_000},
        "ledgers": {
            "ergo": {"payments": {"MU_PER_NANOERG": mu_per_nanoerg}},
            "bitcoin": {"NETWORK": network, "payments": block},
        },
    }


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BitcoinConfigTests(unittest.TestCase):

    def _warnings(self, **kwargs):
        said = []
        validate_bitcoin_config(_config(**kwargs), warn=said.append)
        return said

    def test_no_bitcoin_block_at_all_is_valid(self):
        validate_bitcoin_config({"ledgers": {"ergo": {}}}, warn=None)
        validate_bitcoin_config({}, warn=None)

    def test_an_unknown_network_is_refused(self):
        with self.assertRaisesRegex(ConfigValidationError, "NETWORK must be one of"):
            validate_bitcoin_config(_config(network="dogecoin"), warn=None)

    def test_a_cold_wallet_for_another_network_is_refused(self):
        """Savings are what a cold wallet holds.

        An address valid on mainnet is refused on a testnet node: sweeping to it would
        send funds nobody on this chain can spend, and that is not recoverable.
        """
        with self.assertRaisesRegex(ConfigValidationError, "not a valid testnet"):
            validate_bitcoin_config(
                _config(network="testnet", COLD_WALLET=MAINNET), warn=None
            )
        # And the same address on its own network is fine.
        validate_bitcoin_config(_config(COLD_WALLET=MAINNET), warn=None)
        validate_bitcoin_config(_config(COLD_WALLET=MAINNET_P2SH), warn=None)
        validate_bitcoin_config(
            _config(network="testnet", COLD_WALLET=TESTNET), warn=None
        )

    def test_an_ergo_address_is_not_a_bitcoin_address(self):
        # Each list is checked against its own chain's rules; checking a Bitcoin wallet
        # with Ergo's base58 rules would refuse it for the wrong reason, or accept it.
        with self.assertRaises(ConfigValidationError):
            validate_bitcoin_config(
                _config(COLD_WALLET="9gGZp7HRAFxgGWSwvS4hCbxM2RpkYr6pHvwpU4GPrpvxY7Y2nQo"),
                warn=None,
            )

    def test_a_malformed_btc_amount_is_refused(self):
        for key in ("HOT_WALLET_LIMITS", "COLD_WALLET_MIN_TRANSFER",
                    "DONATION_MIN_TRANSFER"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigValidationError):
                    validate_bitcoin_config(_config(**{key: "0.000000001"}), warn=None)

    def test_a_donation_wallet_is_checked_against_bitcoins_rules(self):
        validate_bitcoin_config(
            _config(DONATION_WALLETS=[{"address": MAINNET, "weight": 1}]), warn=None
        )
        with self.assertRaisesRegex(ConfigValidationError, "not a valid Bitcoin"):
            validate_bitcoin_config(
                _config(DONATION_WALLETS=[{"address": TESTNET, "weight": 1}]), warn=None
            )

    def test_a_non_positive_rate_is_refused(self):
        for rate in ("0", "-1"):
            with self.subTest(rate=rate):
                with self.assertRaises(ConfigValidationError):
                    validate_bitcoin_config(_config(rate=rate), warn=None)

    def test_an_unreadable_rate_is_refused(self):
        with self.assertRaisesRegex(ConfigValidationError, "must be a number"):
            validate_bitcoin_config(_config(rate="soon"), warn=None)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RateScaleWarningTests(unittest.TestCase):
    """The scales check, and the two things it must not do.

    It must catch a rate borrowed from the other chain by analogy, which misprices the
    node by six orders of magnitude. And it must stay silent on a correct config: a
    warning that sounds on the shipped defaults trains operators to ignore warnings.
    """

    def _warnings(self, **kwargs):
        said = []
        validate_bitcoin_config(_config(**kwargs), warn=said.append)
        return said

    def test_the_shipped_default_says_nothing(self):
        # An unset rate is a working state: the node does not offer Bitcoin.
        self.assertEqual(self._warnings(rate=""), [])

    def test_a_realistic_rate_says_nothing(self):
        # ERG at $0.50 and BTC at $100,000, with MU_PER_NANOERG at 1.
        self.assertEqual(self._warnings(rate=2_000_000), [])

    def test_the_rate_copied_from_ergo_is_named(self):
        said = self._warnings(rate=1)
        self.assertTrue(any("MU_PER_NANOERG's value" in w for w in said), said)

    def test_two_rates_that_imply_an_impossible_market_are_named(self):
        """No price feed needed: the two rates imply a BTC/ERG price by themselves.

        One BTC is 1e8 satoshi and one ERG is 1e9 nanoERG, so the ratio of the two
        rates *is* this node's opinion about what a BTC is worth in ERG. A config
        saying one BTC is worth less than one ERG is not a market view.
        """
        said = self._warnings(rate=5)
        self.assertTrue(any("worth" in w and "ERG" in w for w in said), said)

    def test_a_node_that_prices_nothing_in_erg_is_not_second_guessed(self):
        # With no Ergo rate configured there is no ratio to disbelieve.
        config = _config(rate=1)
        config["ledgers"]["ergo"]["payments"]["MU_PER_NANOERG"] = ""
        said = []
        validate_bitcoin_config(config, warn=said.append)
        self.assertFalse(any("worth" in w and "BTC is worth" in w for w in said), said)

    def test_a_dormant_ledger_never_warns_about_its_donations(self):
        # A node that has not turned Bitcoin on must not be told at every startup about
        # a donation it will never pay.
        self.assertEqual(
            self._warnings(rate="", DONATION_PERCENTAGE="0.02", DONATION_WALLETS=[]), []
        )
        self.assertTrue(
            self._warnings(rate=2_000_000, DONATION_PERCENTAGE="0.02",
                           DONATION_WALLETS=[])
        )


if __name__ == "__main__":
    unittest.main()
