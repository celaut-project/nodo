"""The on-chain reputation term: what it costs to buy, and what it refuses to sell.

The balancer weighs what the ledgers say, and the only interesting question about that is
economic: an opinion is worth ``share x burned ERG``, minting a proof is free, and the ERG
behind one can never come back out. The burn is counted here -- what is not granted is
that a burn means anything on its own. A reputation proof is not a peer, so rather than
asking who owns a proof (free to answer wrongly: proofs and wallets both cost nothing),
this node asks what the proof has *said*, and scores it against what we have seen
ourselves.

Every property pinned here is a way the term could be bought, and the assertion that it
cannot be:

* a proof that agrees with us about nothing weighs **zero**, whatever it burned -- which
  is the default, and what closes the mint-a-second-proof trick;
* a proof that contradicts what we have seen is **silenced, not inverted** -- paying a
  proof to denounce a rival must not promote the rival;
* agreement is about **direction, not volume**: staking more loudly is not agreeing more;
* **no single proof** carries the term, however funded and however agreeable;
* the subject's **own proofs** are set aside before anything is summed (issue #351), and
  so are ours, which agree with us by construction;
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
        NANOERG_PER_ERG,
        agreement,
        contribution,
        standing,
        standing_by_peer,
    )
    from src.utils.config_validation import (
        ConfigValidationError,
        validate_balancers_config,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

W_D = 0.3
HALF_LOCAL = 50.0

# Spelled out rather than imported, so this module still *loads* when the import above
# failed and every case degrades to a skip. A default argument is evaluated at import
# time, which turns a missing dependency into a NameError that takes the whole file down.
# `ConfigValidationTests` is where these are pinned to what the code ships.
CAP_ERG = 5.0
NANOERG = 10 ** 9


def row(subject="peer-a", proof="proof-1", verdict=1.0, burned_erg=CAP_ERG):
    return {
        "ledger": "ergo",
        "subject_id": subject,
        "proof_id": proof,
        "verdict": verdict,
        "burned_nanoerg": int(burned_erg * NANOERG),
    }


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AgreementTests(unittest.TestCase):
    """What a proof's opinion is worth here is how far it matches what we have seen."""

    def test_a_proof_we_share_no_ground_with_is_inaudible(self):
        """The defence, in one assertion, and it is the *default*.

        A freshly minted proof has only ever spoken about the node that minted it, so it
        overlaps with our own opinions nowhere. Zero, at any burn -- which is what stops
        the second-proof trick: mint a proof nobody knows, burn into it, have it stake
        100 % of itself on your own node.
        """
        self.assertEqual(agreement({"peer-a": 1.0}, {"peer-b": 0.9}), 0.0)
        self.assertEqual(agreement({}, {"peer-b": 0.9}), 0.0)
        self.assertEqual(agreement({"peer-a": 1.0}, {}), 0.0)

    def test_a_proof_that_vouches_for_a_peer_that_failed_us_is_discredited_by_it(self):
        """The case the whole shape exists for.

        Not silenced by a rule about its owner -- contradicted by the evidence. The proof
        stakes itself on a peer we have seen fail, and that vouch is the thing that makes
        it worthless to us.
        """
        self.assertEqual(agreement({"peer-a": 1.0}, {"peer-a": -0.9}), 0.0)

    def test_disagreement_silences_and_never_inverts(self):
        """Clamped at zero, not sign-preserving -- and the difference is an attack.

        Were disagreement to flip the sign of what a proof publishes, paying a proof to
        speak *against* a rival would promote that rival, and denouncing a peer we trust
        would be a way to have that peer promoted. A weapon pointed wherever the attacker
        likes, for the price of a burn.
        """
        self.assertEqual(agreement({"peer-a": -1.0, "peer-b": 1.0},
                                   {"peer-a": 0.8, "peer-b": -0.8}), 0.0)
        self.assertGreaterEqual(
            agreement({"peer-a": -1.0}, {"peer-a": -0.8}), 0.99
        )

    def test_full_agreement_scores_one_and_partial_agreement_scores_between(self):
        both = agreement({"peer-a": 1.0, "peer-b": -1.0},
                         {"peer-a": 0.5, "peer-b": -0.5})
        one_of_two = agreement({"peer-a": 1.0, "peer-b": 1.0},
                               {"peer-a": 0.5, "peer-b": -0.5})
        self.assertAlmostEqual(both, 1.0)
        self.assertEqual(one_of_two, 0.0)
        self.assertLess(
            agreement({"peer-a": 1.0, "peer-b": 0.2},
                      {"peer-a": 0.5, "peer-b": -0.1}),
            1.0,
        )

    def test_agreement_is_about_direction_not_volume(self):
        """Staking more loudly is not agreeing more.

        Magnitude is what the burn and the cap price. Letting it in here as well would
        make a proof that stakes heavily on many peers look more *credible* for doing so,
        which is a second way to buy the same thing.
        """
        quiet = agreement({"peer-a": 0.1, "peer-b": -0.1}, {"peer-a": 0.5, "peer-b": -0.5})
        loud = agreement({"peer-a": 1.0, "peer-b": -1.0}, {"peer-a": 0.5, "peer-b": -0.5})
        self.assertAlmostEqual(quiet, loud)

    def test_a_peer_we_have_no_opinion_on_neither_helps_nor_dilutes(self):
        """Unverifiable claims are left out of the comparison entirely.

        Not counted as agreement, which would be inventing evidence, and not counted
        against it either: a proof that also rates a hundred peers we have never met is
        not less credible about the one we both know.
        """
        checked_only = agreement({"peer-a": 1.0}, {"peer-a": 0.5})
        with_strangers = agreement(
            {"peer-a": 1.0, "stranger-1": 1.0, "stranger-2": -1.0},
            {"peer-a": 0.5, "stranger-1": 0.0},
        )
        self.assertAlmostEqual(checked_only, 1.0)
        self.assertAlmostEqual(with_strangers, 1.0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BurnTests(unittest.TestCase):
    """The burn counts -- but only as far as the proof behind it agrees with us."""

    def test_a_burn_behind_a_proof_we_share_no_ground_with_buys_nothing(self):
        self.assertEqual(
            contribution(1.0, 10 ** 6 * NANOERG_PER_ERG, 0.0, DEFAULT_PUBLISHER_CAP),
            0.0,
        )
        self.assertEqual(
            standing([row(burned_erg=10 ** 6)], local_scores={"peer-b": 0.9}),
            {},
        )

    def test_a_larger_burn_says_more_up_to_the_cap(self):
        small = contribution(1.0, 1 * NANOERG_PER_ERG, 1.0, DEFAULT_PUBLISHER_CAP)
        larger = contribution(1.0, 3 * NANOERG_PER_ERG, 1.0, DEFAULT_PUBLISHER_CAP)
        self.assertAlmostEqual(small, 1.0)
        self.assertAlmostEqual(larger, 3.0)

    def test_the_share_is_what_is_multiplied_not_the_token_count(self):
        """``Opinion.backed_nanoerg``'s rule: half a proof behind you is half its burn.

        Multiplying the raw token amount instead would reward minting a larger supply,
        which costs nothing.
        """
        self.assertAlmostEqual(
            contribution(0.5, 4 * NANOERG_PER_ERG, 1.0, DEFAULT_PUBLISHER_CAP), 2.0
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PublisherCapTests(unittest.TestCase):

    def test_one_proof_cannot_carry_the_term(self):
        """Concave per publisher: burning more than the cap buys exactly the cap."""
        at_the_cap = contribution(
            1.0, DEFAULT_PUBLISHER_CAP * NANOERG_PER_ERG, 1.0, DEFAULT_PUBLISHER_CAP
        )
        far_past_it = contribution(
            1.0, 10 ** 6 * NANOERG_PER_ERG, 1.0, DEFAULT_PUBLISHER_CAP
        )
        self.assertEqual(at_the_cap, DEFAULT_PUBLISHER_CAP)
        self.assertEqual(at_the_cap, far_past_it)

    def test_the_cap_bounds_the_magnitude_and_keeps_the_sign(self):
        """A proof staking everything *against* a peer is worth hearing, once."""
        self.assertEqual(
            contribution(-1.0, 10 ** 6 * NANOERG_PER_ERG, 1.0, DEFAULT_PUBLISHER_CAP),
            -DEFAULT_PUBLISHER_CAP,
        )

    def test_reaching_the_half_credit_takes_several_agreeing_proofs(self):
        """Four proofs at the cap, in full agreement with us. A coalition, not a buy."""
        rows = [row(proof=f"proof-{i}") for i in range(4)]
        rows += [row(subject="peer-good", proof=f"proof-{i}") for i in range(4)]
        result = standing(rows, local_scores={"peer-good": 0.9})
        self.assertAlmostEqual(result["peer-a"], 0.5)

    def test_one_proof_alone_reaches_a_fifth_of_the_ceiling(self):
        rows = [row(), row(subject="peer-good")]
        result = standing(rows, local_scores={"peer-good": 0.9})
        self.assertAlmostEqual(
            result["peer-a"],
            DEFAULT_PUBLISHER_CAP / (DEFAULT_PUBLISHER_CAP + DEFAULT_ONCHAIN_HALF_CREDIT),
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SaturationTests(unittest.TestCase):

    def test_half_the_weight_is_earned_at_the_half_credit(self):
        self.assertAlmostEqual(
            reputation_factor(DEFAULT_ONCHAIN_HALF_CREDIT, DEFAULT_ONCHAIN_HALF_CREDIT),
            0.5,
        )

    def test_it_saturates_without_reaching_one(self):
        rows = [row(proof=f"proof-{i}") for i in range(1000)]
        rows += [row(subject="peer-good", proof=f"proof-{i}") for i in range(1000)]
        value = standing(rows, local_scores={"peer-good": 0.9})["peer-a"]
        self.assertLess(value, 1.0)
        self.assertGreater(value, 0.99)

    def test_it_saturates_downwards_too(self):
        rows = [row(proof=f"proof-{i}", verdict=-1.0) for i in range(1000)]
        rows += [row(subject="peer-good", proof=f"proof-{i}") for i in range(1000)]
        value = standing(rows, local_scores={"peer-good": 0.9})["peer-a"]
        self.assertGreater(value, -1.0)
        self.assertLess(value, -0.99)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OwnVoiceExclusionTests(unittest.TestCase):
    """Neither the subject's own proofs nor ours are allowed to speak here."""

    def test_the_subjects_own_proofs_are_split_off_before_anything_is_summed(self):
        """Issue #351. Hygiene, not the defence.

        The announced list is what the subject *chose* to disclose, and a proof it never
        mentions costs nothing to mint. What stops the undisclosed proof is that it has
        no agreement with us either -- ``AgreementTests`` is where that lives.
        """
        from src.reputation_system import onchain_indexer
        from src.reputation_system.opinions import Opinion

        def opinion(proof):
            return Opinion(
                ledger="ergo", proof_id=proof, owner="0008cd" + "02" * 33,
                amount=1, assigned_amount=1, positive=True, published_at=0,
                box_id="b", burned_nanoerg=10 ** 12,
            )

        collected = [opinion("own-proof"), opinion("other-proof")]

        with mock.patch(
            "src.reputation_system.interface._opinion_readers",
            return_value={"ergo": lambda node_id: collected},
        ), mock.patch(
            "src.reputation_system.interface._own_proof_ids",
            return_value=("own-proof",),
        ):
            rows = onchain_indexer.opinions_for("peer-a")

        self.assertEqual([proof for proof, _, _ in rows], ["other-proof"])

    def test_our_own_proof_agrees_with_us_by_construction_and_is_dropped(self):
        """``submit_to_ledger`` publishes our local scores, so ours is a perfect mirror.

        Counting it would weigh one observation twice: once as the local term nobody can
        buy, and once through the channel that is for sale.
        """
        rows = [row(proof="ours"), row(subject="peer-good", proof="ours")]
        local = {"peer-good": 0.9}
        self.assertIn("peer-a", standing(rows, local_scores=local))
        self.assertEqual(
            standing(rows, local_scores=local, excluded_proof_ids=("ours",)), {}
        )


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

    def test_an_unreadable_local_score_does_not_take_half_the_table_with_it(self):
        with mock.patch(
            "src.database.sql_connection.SQLConnection.get_onchain_opinions",
            return_value=[row(), row(subject="peer-b")],
        ), mock.patch(
            "src.database.sql_connection.SQLConnection.get_peers_id",
            return_value=["peer-a", "peer-b"],
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
            return_value=[row(), row(subject="peer-good")],
        ), mock.patch(
            "src.database.sql_connection.SQLConnection.get_peers_id",
            return_value=["peer-a", "peer-good"],
        ), mock.patch(
            "src.reputation_system.interface.compute_reputation",
            side_effect=lambda peer_id: 10 ** 6 if peer_id == "peer-good" else 0,
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
        ceiling -- every proof in the network burning without limit and in full agreement
        with us -- `ONCHAIN_REPUTATION_WEIGHT` of log-space bonus. Donating buys, at its
        own ceiling, `DONATION_WEIGHT`. The first must not exceed the second, or the
        rational play is to destroy the money instead of funding the software.
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

    def test_the_whole_onchain_ceiling_is_worth_under_three_donated_erg(self):
        """The mirror attack's payoff, priced.

        Agreement can be copied off the chain, so the honest way to read the term is:
        what does an attacker who reaches perfect agreement and burns without limit get?
        The answer is the ceiling, and the ceiling is what donating 2.5 ERG buys. The
        bounding is the safety, not the agreement score.
        """
        donated_erg = 2.5
        half_credit_erg = 5.0
        donation_bonus = W_D * (donated_erg / (donated_erg + half_credit_erg))
        self.assertAlmostEqual(DEFAULT_ONCHAIN_WEIGHT, donation_bonus)

    def test_a_realistic_burn_is_far_below_even_one_donated_erg(self):
        """Not just the ceilings: the achievable case.

        One proof at the cap, in full agreement with us, is S = 5 -> o = 0.2, worth 0.02
        in log space. A single ERG donated is d = 1/6, worth 0.05. So a burn routed
        through one agreeable proof is worth less than 1 ERG donated.
        """
        rows = [row(), row(subject="peer-good")]
        one_proof = standing(rows, local_scores={"peer-good": 0.9})["peer-a"]
        self.assertLess(one_proof * DEFAULT_ONCHAIN_WEIGHT, (1 / 6) * W_D)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfigValidationTests(unittest.TestCase):

    def test_the_shipped_balancers_block_is_valid(self):
        validate_balancers_config({"balancers": {
            "SOCIALIZATION_FACTOR": 2, "REPUTATION_HALF_CREDIT": 50,
            "COST_AVERAGE_VARIATION": 1, "DONATION_WEIGHT": 0.3,
            "DONATION_HALF_CREDIT": "5000000000", "DONATION_AGE_SCALE": 31536000,
            "LOCAL_BIAS": 1.0, "ONCHAIN_REPUTATION_WEIGHT": 0.1,
            "ONCHAIN_REPUTATION_HALF_CREDIT": 20.0, "ONCHAIN_PUBLISHER_CAP": 5.0,
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

    def test_the_numbers_this_file_reasons_with_are_the_ones_that_ship(self):
        self.assertEqual(CAP_ERG, DEFAULT_PUBLISHER_CAP)
        self.assertEqual(NANOERG, NANOERG_PER_ERG)

    def test_the_example_config_ships_the_defaults_the_code_assumes(self):
        """The two must not drift: the docs quote one set of numbers for both."""
        import os

        import yaml

        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "config.example.yaml"), encoding="utf-8") as handle:
            balancers = (yaml.safe_load(handle) or {})["balancers"]
        self.assertEqual(
            float(balancers["ONCHAIN_REPUTATION_WEIGHT"]), DEFAULT_ONCHAIN_WEIGHT
        )
        self.assertEqual(
            float(balancers["ONCHAIN_REPUTATION_HALF_CREDIT"]), DEFAULT_ONCHAIN_HALF_CREDIT
        )
        self.assertEqual(
            float(balancers["ONCHAIN_PUBLISHER_CAP"]), DEFAULT_PUBLISHER_CAP
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
