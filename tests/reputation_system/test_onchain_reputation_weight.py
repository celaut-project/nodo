"""The on-chain reputation term: what it costs to buy, and what it refuses to sell.

The balancer now weighs what the ledgers say, and the only interesting question about
that is economic: an opinion is worth ``share x burned ERG``, minting a proof is free, and
the ERG behind one can never come back out. So every property pinned here is a way the
term could be bought, and the assertion that it cannot be:

* a publisher this node has never dealt with weighs **zero**, whatever it burned;
* a publisher this node distrusts is **silenced, not inverted** -- paying a distrusted
  peer to badmouth a rival must not promote the rival;
* **no single publisher** carries the term, however trusted;
* the subject's **own proofs** are set aside before anything is summed (issue #351);
* a failed read scores **every** candidate zero, never some of them (issue #352's rule,
  applied to a second term);
* and the shipped weight is at or below ``DONATION_WEIGHT``, so the same ERG spent on a
  burn never beats what it buys as a donation.

No network and no database: the pure functions take their inputs, and the two readers are
stubbed. Issue #353.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    # Before anything that builds a ConfigManager at import: the shipped example points
    # STORAGE at /nodo, which only exists on an installed node.
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.balancers.scoring import reputation_factor, score
    from src.reputation_system import onchain_credit
    from src.reputation_system.onchain_credit import (
        DEFAULT_ONCHAIN_HALF_CREDIT,
        DEFAULT_ONCHAIN_WEIGHT,
        DEFAULT_PUBLISHER_CAP,
        contribution,
        standing,
        standing_by_peer,
        trust,
    )
    from src.utils.config_validation import (
        ConfigValidationError,
        validate_balancers_config,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

W_D = 0.3
HALF_LOCAL = 50.0


def row(subject="peer-a", proof="proof-1", publisher="pub-1", verdict=1.0):
    return {
        "ledger": "ergo",
        "subject_id": subject,
        "proof_id": proof,
        "publisher_peer_id": publisher,
        "verdict": verdict,
    }


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PublisherTrustTests(unittest.TestCase):
    """What a publisher's opinion is worth here is what *we* think of the publisher."""

    def test_a_publisher_we_have_never_dealt_with_is_inaudible(self):
        """The defence, in one assertion.

        A self-funded proof belongs to no peer this node has transacted with. Its
        opinion is worth nothing here at any price, which is what stops the second-proof
        trick: mint a proof nobody knows, burn into it, have it stake 100 % of itself on
        your own node.
        """
        self.assertEqual(trust(0.0, HALF_LOCAL), 0.0)
        self.assertEqual(contribution(1.0, trust(0.0, HALF_LOCAL), DEFAULT_PUBLISHER_CAP), 0.0)

    def test_an_unknown_publisher_weighs_zero_however_loud_its_verdict(self):
        self.assertEqual(
            standing([row(publisher="stranger")], trust_by_peer={}),
            {},
        )

    def test_a_trusted_publisher_is_heard_in_proportion_to_our_own_score(self):
        low = trust(10, HALF_LOCAL)
        high = trust(500, HALF_LOCAL)
        self.assertGreater(high, low)
        self.assertLess(high, 1.0)
        self.assertAlmostEqual(trust(50, HALF_LOCAL), 0.5)

    def test_a_distrusted_publisher_is_silenced_and_never_inverted(self):
        """``max(0, ...)``, not sign-preserving -- and the difference is an attack.

        Were a negative local score to flip the sign of what that peer publishes, paying
        a peer we already distrust to speak *against* a rival would promote the rival,
        and speaking against yourself from a burner proof would raise your own standing.
        """
        self.assertEqual(trust(-10 ** 6, HALF_LOCAL), 0.0)
        self.assertEqual(
            contribution(-1.0, trust(-500, HALF_LOCAL), DEFAULT_PUBLISHER_CAP), 0.0
        )

    def test_the_burn_is_nowhere_in_the_arithmetic(self):
        # Not an assertion about a number: `standing` takes no backing figure at all, so
        # there is no argument through which a sacrifice could enter.
        import inspect

        signature = inspect.signature(standing)
        self.assertNotIn("burned", " ".join(signature.parameters))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PublisherCapTests(unittest.TestCase):

    def test_one_publisher_cannot_carry_the_term(self):
        """Concave per publisher: staking all of itself buys the cap, and no more."""
        full = contribution(1.0, 1.0, DEFAULT_PUBLISHER_CAP)
        quarter = contribution(0.25, 1.0, DEFAULT_PUBLISHER_CAP)
        self.assertEqual(full, DEFAULT_PUBLISHER_CAP)
        self.assertEqual(full, quarter)

    def test_below_the_cap_a_larger_stake_still_says_more(self):
        self.assertLess(
            contribution(0.1, 1.0, DEFAULT_PUBLISHER_CAP),
            contribution(0.2, 1.0, DEFAULT_PUBLISHER_CAP),
        )

    def test_the_cap_bounds_the_magnitude_and_keeps_the_sign(self):
        self.assertEqual(
            contribution(-1.0, 1.0, DEFAULT_PUBLISHER_CAP), -DEFAULT_PUBLISHER_CAP
        )

    def test_reaching_the_half_credit_takes_several_trusted_publishers(self):
        """Four fully-trusted publishers at the cap, which is a coalition, not a buy."""
        rows = [
            row(proof=f"proof-{i}", publisher=f"pub-{i}", verdict=1.0) for i in range(4)
        ]
        trusted = {f"pub-{i}": 1.0 for i in range(4)}
        result = standing(
            rows, trust_by_peer=trusted, cap=DEFAULT_PUBLISHER_CAP,
            half=DEFAULT_ONCHAIN_HALF_CREDIT,
        )
        self.assertAlmostEqual(result["peer-a"], 0.5)

    def test_one_publisher_alone_reaches_a_fifth_of_the_ceiling(self):
        result = standing(
            [row()], trust_by_peer={"pub-1": 1.0}, cap=DEFAULT_PUBLISHER_CAP,
            half=DEFAULT_ONCHAIN_HALF_CREDIT,
        )
        self.assertAlmostEqual(result["peer-a"], 0.25 / 1.25)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SaturationTests(unittest.TestCase):

    def test_half_the_weight_is_earned_at_the_half_credit(self):
        self.assertAlmostEqual(reputation_factor(1.0, DEFAULT_ONCHAIN_HALF_CREDIT), 0.5)

    def test_it_saturates_without_reaching_one(self):
        rows = [
            row(proof=f"proof-{i}", publisher=f"pub-{i}") for i in range(1000)
        ]
        trusted = {f"pub-{i}": 1.0 for i in range(1000)}
        value = standing(rows, trust_by_peer=trusted)["peer-a"]
        self.assertLess(value, 1.0)
        self.assertGreater(value, 0.99)

    def test_it_saturates_downwards_too(self):
        rows = [
            row(proof=f"proof-{i}", publisher=f"pub-{i}", verdict=-1.0)
            for i in range(1000)
        ]
        trusted = {f"pub-{i}": 1.0 for i in range(1000)}
        value = standing(rows, trust_by_peer=trusted)["peer-a"]
        self.assertGreater(value, -1.0)
        self.assertLess(value, -0.99)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SelfProofExclusionTests(unittest.TestCase):
    """The subject's own voice never reaches the index (issue #351).

    Hygiene, not the defence -- the list is what the subject chose to announce, and
    minting a proof it never mentions is free. What actually stops the unannounced proof
    is that it belongs to no peer, so ``PublisherTrustTests`` is where that lives.
    """

    def test_the_subjects_own_proofs_are_split_off_before_anything_is_summed(self):
        from src.reputation_system import onchain_indexer
        from src.reputation_system.opinions import Opinion

        def opinion(proof):
            return Opinion(
                ledger="ergo", proof_id=proof, owner="0008cd" + "02" * 33,
                amount=1, assigned_amount=1, positive=True, published_at=0,
                box_id="b", burned_nanoerg=10 ** 12,
            )

        collected = [opinion("own-proof"), opinion("other-proof")]
        owners = {"own-proof": "peer-a", "other-proof": "pub-1"}

        with mock.patch(
            "src.reputation_system.interface._opinion_readers",
            return_value={"ergo": lambda node_id: collected},
        ), mock.patch(
            "src.reputation_system.interface._own_proof_ids",
            return_value=("own-proof",),
        ):
            rows = onchain_indexer.opinions_for("peer-a", owners)

        self.assertEqual([proof for proof, _, _ in rows], ["other-proof"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FailureZeroesEveryCandidateTests(unittest.TestCase):
    """Never some of them. The donation term's promise, kept by this one too."""

    def setUp(self):
        onchain_credit.forget_cached_standings()

    def tearDown(self):
        onchain_credit.forget_cached_standings()

    def test_an_unreadable_index_reads_as_nobody_being_vouched_for(self):
        with mock.patch(
            "src.database.sql_connection.SQLConnection.get_onchain_opinions",
            side_effect=RuntimeError("database is locked"),
        ):
            self.assertEqual(standing_by_peer(), {})

    def test_an_unreadable_publisher_score_does_not_take_half_the_table_with_it(self):
        with mock.patch(
            "src.database.sql_connection.SQLConnection.get_onchain_opinions",
            return_value=[row(), row(subject="peer-b", publisher="pub-2")],
        ), mock.patch(
            "src.reputation_system.interface.compute_reputation",
            side_effect=RuntimeError("no such column"),
        ):
            self.assertEqual(standing_by_peer(), {})

    def test_a_failure_is_never_cached(self):
        with mock.patch(
            "src.database.sql_connection.SQLConnection.get_onchain_opinions",
            side_effect=RuntimeError("database is locked"),
        ):
            self.assertEqual(standing_by_peer(), {})
        with mock.patch(
            "src.database.sql_connection.SQLConnection.get_onchain_opinions",
            return_value=[row()],
        ), mock.patch(
            "src.reputation_system.interface.compute_reputation", return_value=10 ** 6
        ):
            self.assertIn("peer-a", standing_by_peer())

    def test_an_empty_index_is_not_an_error_and_credits_nobody(self):
        with mock.patch(
            "src.database.sql_connection.SQLConnection.get_onchain_opinions",
            return_value=[],
        ):
            self.assertEqual(standing_by_peer(), {})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ExchangeRateTests(unittest.TestCase):
    """The economics issue #353 exists to protect: donating must stay the better buy."""

    def test_the_shipped_onchain_weight_does_not_exceed_the_donation_weight(self):
        self.assertLessEqual(DEFAULT_ONCHAIN_WEIGHT, W_D)

    def test_the_config_refuses_a_burn_worth_more_than_a_donation(self):
        with self.assertRaises(ConfigValidationError) as raised:
            validate_balancers_config({"balancers": {
                "ONCHAIN_REPUTATION_WEIGHT": 2, "DONATION_WEIGHT": 0.3,
            }})
        self.assertIn("exchange rate", str(raised.exception))

    def test_equal_weights_are_the_most_that_is_allowed(self):
        validate_balancers_config({"balancers": {
            "ONCHAIN_REPUTATION_WEIGHT": 0.3, "DONATION_WEIGHT": 0.3,
        }})

    def test_the_default_donation_weight_is_what_an_unset_one_is_compared_against(self):
        # An operator who raises only the on-chain weight, leaving DONATION_WEIGHT to its
        # default, must not slip past the check because the key is absent.
        with self.assertRaises(ConfigValidationError):
            validate_balancers_config({"balancers": {"ONCHAIN_REPUTATION_WEIGHT": 1.0}})

    def test_the_same_erg_burned_never_beats_what_it_buys_as_a_donation(self):
        """The substitution the issue is about, priced at the shipped defaults.

        An operator with ERG to spend has two channels. Burning buys, at the absolute
        ceiling -- every publisher in the network fully trusted by us and fully staked --
        `ONCHAIN_REPUTATION_WEIGHT` of log-space bonus. Donating buys, at its own ceiling,
        `DONATION_WEIGHT`. The first must not exceed the second, or the rational play is
        to destroy the money instead of funding the software.
        """
        burned_to_the_ceiling = score(
            cost_mu=1000, onchain_reputation=1.0, onchain_weight=DEFAULT_ONCHAIN_WEIGHT
        )
        donated_to_the_ceiling = score(
            cost_mu=1000, donation_bonus=1.0, donation_weight=W_D
        )
        self.assertLess(burned_to_the_ceiling, donated_to_the_ceiling)

        # And in the units an operator reads: the premium each channel beats.
        plain = score(cost_mu=1000)
        self.assertAlmostEqual(burned_to_the_ceiling - plain, DEFAULT_ONCHAIN_WEIGHT)
        self.assertAlmostEqual(donated_to_the_ceiling - plain, W_D)

    def test_a_realistic_burn_is_far_below_even_one_donated_erg(self):
        """Not just the ceilings: the achievable case.

        One trusted publisher fully staked is the cap, 0.25 -> ô = 0.2, worth 0.02 in log
        space. A single ERG donated is d̂ = 1/6, worth 0.05. So a burn routed through the
        one peer that will speak for you is worth less than 1 ERG donated, before the
        burn's ERG is even counted.
        """
        one_publisher = standing(
            [row()], trust_by_peer={"pub-1": 1.0}, cap=DEFAULT_PUBLISHER_CAP,
            half=DEFAULT_ONCHAIN_HALF_CREDIT,
        )["peer-a"]
        self.assertLess(one_publisher * DEFAULT_ONCHAIN_WEIGHT, (1 / 6) * W_D)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfigValidationTests(unittest.TestCase):

    def test_the_shipped_balancers_block_is_valid(self):
        validate_balancers_config({"balancers": {
            "SOCIALIZATION_FACTOR": 2, "REPUTATION_HALF_CREDIT": 50,
            "COST_AVERAGE_VARIATION": 1, "DONATION_WEIGHT": 0.3,
            "DONATION_HALF_CREDIT": "5000000000", "DONATION_AGE_SCALE": 31536000,
            "LOCAL_BIAS": 1.0, "ONCHAIN_REPUTATION_WEIGHT": 0.1,
            "ONCHAIN_REPUTATION_HALF_CREDIT": 1.0, "ONCHAIN_PUBLISHER_CAP": 0.25,
        }})

    def test_the_example_config_ships_a_valid_block(self):
        import os

        import yaml

        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "config.example.yaml"), encoding="utf-8") as handle:
            shipped = yaml.safe_load(handle) or {}
        validate_balancers_config(shipped)
        self.assertLessEqual(
            float(shipped["balancers"]["ONCHAIN_REPUTATION_WEIGHT"]),
            float(shipped["balancers"]["DONATION_WEIGHT"]),
        )

    def test_a_negative_onchain_weight_is_refused(self):
        with self.assertRaises(ConfigValidationError):
            validate_balancers_config({"balancers": {"ONCHAIN_REPUTATION_WEIGHT": -0.1}})

    def test_a_half_credit_or_cap_of_zero_is_refused_because_the_formula_divides(self):
        for key in ("ONCHAIN_REPUTATION_HALF_CREDIT", "ONCHAIN_PUBLISHER_CAP"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigValidationError):
                    validate_balancers_config({"balancers": {key: 0}})


if __name__ == "__main__":
    unittest.main()
