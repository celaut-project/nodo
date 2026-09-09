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


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NodeServiceBackendTests(unittest.TestCase):
    """`BACKEND: service` needs three things to agree, and none can be guessed.

    Every one of them, missing, leaves a node that boots, advertises Bitcoin and then
    cannot settle a single payment — a failure that would surface as a payout that did
    not happen, on a tick nobody is watching. So they are refused at startup.
    """

    def _config(self, **overrides):
        base = _config(rate="1000")
        bitcoin = base["ledgers"]["bitcoin"]
        bitcoin["BACKEND"] = "service"
        bitcoin["WALLET_KEYS_EXTERNAL"] = False
        bitcoin["WALLET_MNEMONIC"] = "twelve words that are not really twelve words"
        bitcoin["RPC_USER"] = "nodo"
        bitcoin["RPC_PASSWORD"] = "hunter2"
        bitcoin.update(overrides)
        base["core_services"] = {"bitcoin-node": "a1" * 32}
        return base

    def _validate(self, config):
        validate_bitcoin_config(config)

    def test_a_complete_service_configuration_is_accepted(self):
        self._validate(self._config())

    def test_an_unknown_backend_names_all_three(self):
        config = self._config()
        config["ledgers"]["bitcoin"]["BACKEND"] = "electrum"
        with self.assertRaisesRegex(ConfigValidationError, "'core', 'esplora' or 'service'"):
            self._validate(config)

    def test_no_published_service_id_is_refused(self):
        for entry in ({}, {"bitcoin-node": ""}, {"bitcoin-node": "<SET_ME>"}):
            with self.subTest(core_services=entry):
                config = self._config()
                config["core_services"] = entry
                with self.assertRaisesRegex(ConfigValidationError, "core_services.bitcoin-node"):
                    self._validate(config)

    def test_external_keys_and_a_derived_wallet_cannot_both_be_true(self):
        """The flag is what tells the loader whether to mint a mnemonic.

        Left true, the node holds no Bitcoin key and the service comes up with no
        wallet. It is refused rather than silently corrected: "the keys are external"
        honestly means "do not put a key in my config file", and overriding that is not
        a validator's decision to make.
        """
        with self.assertRaisesRegex(ConfigValidationError, "WALLET_KEYS_EXTERNAL"):
            self._validate(self._config(WALLET_KEYS_EXTERNAL=True))

    def test_an_empty_mnemonic_is_accepted_because_the_loader_fills_it_in(self):
        """The setup the documentation asks for, and it must boot.

        Validation runs *before* the loader mints a mnemonic, so refusing an empty one
        would refuse exactly the operator who set `WALLET_KEYS_EXTERNAL: false` and left
        the phrase blank for the node to generate -- with an error telling them to do
        what they had already done. The flag is the invariant; the emptiness is
        transient, and a launch with no wallet is refused later, by name.
        """
        self._validate(self._config(WALLET_MNEMONIC=""))

    def test_credentials_are_required_because_the_cookie_is_unreachable(self):
        # Core writes it inside the service's own filesystem.
        for key in ("RPC_USER", "RPC_PASSWORD"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigValidationError, key):
                    self._validate(self._config(**{key: ""}))

    def test_a_prune_below_cores_own_floor_is_refused(self):
        # bitcoind refuses to start below 550 MiB, which an operator would meet as a
        # service that never comes up.
        with self.assertRaisesRegex(ConfigValidationError, "550"):
            self._validate(self._config(PRUNE_MIB=100))

    def test_zero_means_the_whole_chain_and_is_allowed(self):
        self._validate(self._config(PRUNE_MIB=0))

    def test_a_prune_that_is_not_a_number_is_refused(self):
        with self.assertRaisesRegex(ConfigValidationError, "PRUNE_MIB"):
            self._validate(self._config(PRUNE_MIB="lots"))

    def test_the_other_backends_are_unaffected_by_all_of_this(self):
        # An esplora node still needs no service id, no mnemonic and no credentials.
        config = _config(rate="1000")
        config["ledgers"]["bitcoin"]["BACKEND"] = "esplora"
        self._validate(config)


if __name__ == "__main__":
    unittest.main()
