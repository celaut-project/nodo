"""The donation configuration: what is refused, what is warned about, and where it lives.

Two wallet lists, and the dangerous one is the *count* list -- a bad pay list costs the
operator who set it, a bad count list is paid for by every peer this node routes to. So
both are validated at load rather than trusted, and the failures that would be silent
(a duplicate address doubling a weight, weights that sum to zero, a percentage with
nobody to pay) are the ones pinned here.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    from src.payment_system.donations import config as donation_config
    from src.utils.config import ConfigManager
    from src.utils.config_validation import (
        ConfigValidationError,
        REMOVED_KEYS,
        validate_balancers_config,
        validate_donation_config,
    )
    from src.utils.singleton import Singleton
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

# Two real mainnet P2PK addresses: the checks are structural (base58 + checksum), so a
# made-up string would be refused for the wrong reason.
ADDRESS_A = "9gGZp7HRAFxgGWSwvS4hCbxM2RpkYr6pHvwpU4GPrpvxY7Y2nQo"
ADDRESS_B = "9hHDQb26AjnJUXxcqriqY1mnhpLuUeC81C4pggtK7tupr92Ea1K"


def _payments(**overrides):
    block = {
        "DONATION_PERCENTAGE": "0.02",
        "DONATION_WALLETS": [{"address": ADDRESS_A, "weight": 1.0}],
        "DONATION_CREDIT_WALLETS": [{"address": ADDRESS_A, "weight": 1.0}],
        "DONATION_MIN_TRANSFER": "0.1",
        "DONATION_MIN_CONFIRMATIONS": 10,
    }
    block.update(overrides)
    return block


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DonationValidationTests(unittest.TestCase):

    def _refused(self, **overrides):
        with self.assertRaises(ConfigValidationError) as raised:
            validate_donation_config(_payments(**overrides))
        return str(raised.exception)

    def test_the_shipped_example_config_is_valid(self):
        validate_donation_config(_payments())

    def test_a_percentage_outside_zero_to_one_is_refused(self):
        self.assertIn("share", self._refused(DONATION_PERCENTAGE="1.5"))
        self.assertIn("share", self._refused(DONATION_PERCENTAGE="-0.1"))

    def test_an_invalid_address_is_refused(self):
        message = self._refused(DONATION_WALLETS=[{"address": "not-an-address", "weight": 1}])
        self.assertIn("not a valid Ergo address", message)

    def test_a_negative_weight_is_refused(self):
        """No negative weights, and it is a design constraint rather than a sanity check.

        A negative weight in the count list would turn it into a punishment mechanism,
        and punishing peers is what makes forking the donation code rational.
        """
        self.assertIn("negative", self._refused(
            DONATION_CREDIT_WALLETS=[{"address": ADDRESS_A, "weight": -1}]
        ))

    def test_a_duplicate_address_in_one_list_is_refused(self):
        # Two rows for one address would double its weight without looking like it.
        message = self._refused(DONATION_WALLETS=[
            {"address": ADDRESS_A, "weight": 0.5},
            {"address": ADDRESS_A, "weight": 0.5},
        ])
        self.assertIn("duplicates", message)

    def test_weights_that_are_all_zero_are_refused_rather_than_read_as_an_empty_list(self):
        message = self._refused(DONATION_WALLETS=[
            {"address": ADDRESS_A, "weight": 0},
            {"address": ADDRESS_B, "weight": 0},
        ])
        self.assertIn("all zero", message)

    def test_an_address_may_be_in_both_lists(self):
        # Funding someone you also count is the ordinary case, not a duplicate.
        validate_donation_config(_payments(
            DONATION_WALLETS=[{"address": ADDRESS_A, "weight": 1}],
            DONATION_CREDIT_WALLETS=[{"address": ADDRESS_A, "weight": 1}],
        ))

    def test_a_percentage_with_nobody_to_pay_warns_loudly_instead_of_failing(self):
        # The node runs; it just donates nothing, while the config reads as though it
        # does. A warning, because an operator mid-edit must not be locked out.
        warnings = []
        validate_donation_config(_payments(DONATION_WALLETS=[]), warn=warnings.append)
        self.assertEqual(len(warnings), 1)
        self.assertIn("DONATION_WALLETS is empty", warnings[0])

    def test_no_donation_configured_at_all_is_valid(self):
        validate_donation_config({})

    def test_a_malformed_confirmation_count_is_refused(self):
        self.assertIn("integer", self._refused(DONATION_MIN_CONFIRMATIONS="soon"))

    def test_the_singular_donation_wallet_key_is_gone_for_good(self):
        # Donations became a share of earnings paid to a weighted list. Left in place,
        # the old key would read as configured and donate nothing.
        self.assertIn("DONATION_WALLET", REMOVED_KEYS)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BalancerValidationTests(unittest.TestCase):

    def test_the_shipped_balancers_block_is_valid(self):
        validate_balancers_config({"balancers": {
            "SOCIALIZATION_FACTOR": 2, "REPUTATION_HALF_CREDIT": 50,
            "COST_AVERAGE_VARIATION": 1, "DONATION_WEIGHT": 0.3,
            "DONATION_HALF_CREDIT": "5000000000", "DONATION_AGE_SCALE": 31536000,
            "LOCAL_BIAS": 1.0,
        }})

    def test_a_negative_weight_is_refused(self):
        with self.assertRaises(ConfigValidationError):
            validate_balancers_config({"balancers": {"DONATION_WEIGHT": -0.3}})

    def test_a_half_credit_of_zero_is_refused_because_the_formula_divides_by_it(self):
        for key in ("REPUTATION_HALF_CREDIT", "DONATION_HALF_CREDIT", "DONATION_AGE_SCALE"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigValidationError):
                    validate_balancers_config({"balancers": {key: 0}})

    def test_an_absent_section_is_valid_and_means_the_defaults(self):
        validate_balancers_config({})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfigResolutionTests(unittest.TestCase):
    """The keys resolve from their new home, out of the config this repo ships.

    Against a manager built here on a copy of `config.example.yaml`, not against
    whatever the process happens to have loaded. `ConfigManager` is a singleton that
    other test modules swap out from under each other, so reading the ambient one would
    make these pass or fail on discovery order -- and what is under test is the shipped
    file, which is exactly what a fresh install runs.
    """

    @classmethod
    def setUpClass(cls):
        cls._previous = Singleton._instances.pop(ConfigManager, None)
        load_example_config()

    @classmethod
    def tearDownClass(cls):
        Singleton._instances.pop(ConfigManager, None)
        if cls._previous is not None:
            Singleton._instances[ConfigManager] = cls._previous

    def test_the_moved_keys_resolve_by_their_explicit_paths(self):
        manager = ConfigManager()
        self.assertEqual(float(manager.get("balancers.SOCIALIZATION_FACTOR")), 2.0)
        self.assertEqual(float(manager.get("balancers.COST_AVERAGE_VARIATION")), 1.0)

    def test_the_new_formula_parameters_resolve_too(self):
        manager = ConfigManager()
        self.assertEqual(float(manager.get("balancers.DONATION_WEIGHT")), 0.3)
        self.assertEqual(float(manager.get("balancers.LOCAL_BIAS")), 1.0)
        self.assertEqual(int(manager.get("balancers.DONATION_AGE_SCALE")), 31_536_000)

    def test_the_shipped_config_donates_two_percent_by_default(self):
        # The single change with the largest expected effect in the whole issue, and
        # the one an operator is most likely to be surprised by -- so it is pinned.
        self.assertEqual(str(donation_config.percentage("ergo", "ERG")), "0.02")
        self.assertTrue(donation_config.pay_wallets("ergo"))

    def test_weights_read_back_normalised_for_the_credit_computation(self):
        weights = donation_config.credit_weights("ergo")
        self.assertAlmostEqual(float(sum(weights.values())), 1.0)


if __name__ == "__main__":
    unittest.main()
