"""The on-chain term inside the sort: what it can move, and what it must never move.

``test_onchain_reputation_weight`` pins the arithmetic. This pins the decision -- the
sorter reading the standings under the right key, at the right weight, and failing the
right way. The one that matters most is the last class: at the shipped defaults, the same
ERG spent on a burn must not beat what it buys as a donation, or a rational operator
stops funding the software (issue #353).
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    # Before anything that builds a ConfigManager at import: the shipped example points
    # STORAGE at /nodo, which only exists on an installed node.
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2
    from src.balancers.estimated_cost_sorter import estimated_cost_sorter as sorter
    from src.reputation_system.onchain_credit import DEFAULT_ONCHAIN_WEIGHT
    from src.utils.utils import to_amount
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    sorter = None  # type: ignore[assignment]


def _cost(mu: int) -> "celaut_pb2.EstimatedCost":
    return celaut_pb2.EstimatedCost(
        cost=to_amount(mu),
        init_maintenance_cost=to_amount(0),
        max_maintenance_cost=to_amount(0),
        maintenance_seconds_loop=3600,
        variance=0.0,
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OnChainStandingInTheSortTests(unittest.TestCase):

    def _order(self, standings, candidates, donations=None):
        with mock.patch.object(sorter, "bonus_by_peer", return_value=donations or {}), \
                mock.patch.object(sorter, "compute_reputation", return_value=0.0), \
                mock.patch.object(sorter, "standing_by_peer", return_value=standings):
            return [peer_id for peer_id, _ in sorter.estimated_cost_sorter(candidates)]

    def test_a_vouched_for_peer_beats_an_equally_priced_stranger(self):
        candidates = {"peer-a": _cost(1000), "peer-b": _cost(1000)}
        self.assertEqual(self._order({"peer-b": 0.9}, candidates)[0], "peer-b")

    def test_a_peer_the_network_stakes_against_is_still_penalised(self):
        """Sign-preserving, like the local term and unlike donations.

        An accusation is the one thing sunk cost is genuinely good at making expensive,
        so the term carries it -- but only from proofs whose other opinions match what
        this node has seen for itself, and only up to the cap.
        """
        candidates = {"peer-a": _cost(1000), "peer-b": _cost(1000)}
        self.assertEqual(self._order({"peer-b": -0.9}, candidates)[0], "peer-a")

    def test_a_peer_only_strangers_vouch_for_is_ranked_on_its_price(self):
        candidates = {"cheap": _cost(100), "vouched": _cost(1000)}
        self.assertEqual(self._order({"vouched": 0.99}, candidates)[0], "cheap")

    def test_the_term_never_outweighs_the_home_field_preference(self):
        # LOCAL_BIAS is 1.0 and the whole on-chain ceiling is 0.1: a peer the network
        # loves does not pull work off this node at the same price.
        candidates = {"local": _cost(1000), "peer-a": _cost(1000)}
        self.assertEqual(self._order({"peer-a": 1.0}, candidates)[0], "local")

    def test_this_node_is_never_credited_with_what_the_chain_says_about_it(self):
        """What the ledgers say about us is what we published (``submit_to_ledger``).

        Counting it here would let an operator raise their own rank against their peers
        by burning ERG into their own proof -- and would count one local observation
        twice, once unpurchasably and once through a channel that is for sale.
        """
        from src.database.sql_connection import LOCAL_PEER_ID

        candidates = {"local": _cost(3000), "peer-a": _cost(1000)}
        for key in ("local", LOCAL_PEER_ID):
            with self.subTest(key=key):
                self.assertEqual(
                    self._order({key: 1.0}, candidates)[0],
                    "peer-a",
                    "our own on-chain standing must not rank us",
                )

    def test_no_standings_anywhere_leaves_the_existing_policy_untouched(self):
        """The regression guard: without an index, routing is exactly what it was.

        Local tolerates a price up to e**1 (~2.7x) higher than a peer's; at 3x it
        delegates. An index that has never been filled must not change that by a hair.
        """
        self.assertEqual(
            self._order({}, {"local": _cost(2500), "peer-a": _cost(1000)})[0], "local"
        )
        self.assertEqual(
            self._order({}, {"local": _cost(3000), "peer-a": _cost(1000)})[0], "peer-a"
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FailureZeroesEveryCandidateTests(unittest.TestCase):
    """Every candidate, never some of them -- the donation term's rule, kept here too.

    A half-filled index would silently favour whichever peers happen to be cached, which
    is worse than having no term at all: it is a routing decision made on the shape of a
    failure.
    """

    def test_an_unreadable_index_ranks_purely_on_price(self):
        candidates = {"peer-a": _cost(100), "peer-b": _cost(1000)}
        with mock.patch.object(sorter, "bonus_by_peer", return_value={}), \
                mock.patch.object(sorter, "compute_reputation", return_value=0.0), \
                mock.patch(
                    "src.database.sql_connection.SQLConnection.get_onchain_opinions",
                    side_effect=RuntimeError("database is locked"),
                ):
            order = [peer_id for peer_id, _ in sorter.estimated_cost_sorter(candidates)]
        self.assertEqual(order, ["peer-a", "peer-b"])

    def test_the_sort_survives_an_index_that_raises_and_does_not_take_the_launch_down(self):
        # The sort is eager (PEP 289), so anything that raises while scoring takes every
        # candidate with it and surfaces in `launch_service` as a bare StopIteration --
        # which is what issue #352 was. Patched at the database so the real read runs.
        from src.reputation_system import onchain_credit

        onchain_credit.forget_cached_standings()
        try:
            with mock.patch.object(sorter, "bonus_by_peer", return_value={}), \
                    mock.patch.object(sorter, "compute_reputation", return_value=0.0), \
                    mock.patch(
                        "src.database.sql_connection.SQLConnection.get_onchain_opinions",
                        side_effect=RuntimeError("no such table: onchain_opinions"),
                    ):
                order = [
                    peer_id for peer_id, _ in sorter.estimated_cost_sorter(
                        {"local": _cost(1000), "a": _cost(10), "b": _cost(100)}
                    )
                ]
        finally:
            onchain_credit.forget_cached_standings()
        self.assertEqual(order, ["a", "b", "local"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BurningMustNotBeatDonatingTests(unittest.TestCase):
    """The economics of issue #353, reproduced as a routing decision.

    Two peers, same price. One bought the best on-chain standing that exists; the other
    donated. The donor has to win, at the shipped defaults, or the rational play is to
    destroy money instead of funding the software every node here runs on.
    """

    def _order(self, standings, donations, candidates):
        with mock.patch.object(sorter, "bonus_by_peer", return_value=donations), \
                mock.patch.object(sorter, "compute_reputation", return_value=0.0), \
                mock.patch.object(sorter, "standing_by_peer", return_value=standings):
            return [peer_id for peer_id, _ in sorter.estimated_cost_sorter(candidates)]

    def test_a_maximum_burn_loses_to_a_maximum_donation(self):
        candidates = {"burner": _cost(1000), "donor": _cost(1000)}
        order = self._order({"burner": 1.0}, {"donor": 1.0}, candidates)
        self.assertEqual(order[0], "donor")

    def test_a_maximum_burn_loses_even_to_a_partial_donation(self):
        """Five ERG donated -- the half credit, d̂ = 0.5 -> 0.15 -- still beats the
        entire on-chain ceiling of 0.1. There is no amount of ERG that buys past it."""
        candidates = {"burner": _cost(1000), "donor": _cost(1000)}
        order = self._order({"burner": 1.0}, {"donor": 0.5}, candidates)
        self.assertEqual(order[0], "donor")

    def test_the_ceiling_of_the_burn_is_the_weight_and_nothing_more(self):
        """A weight is the maximum equivalent price discount, since price is a log.

        At 0.1, the best conceivable on-chain standing beats a price up to e**0.1 (about
        10.5 %) higher, and loses to anything cheaper than that.
        """
        from math import exp

        self.assertAlmostEqual(exp(DEFAULT_ONCHAIN_WEIGHT), 1.1051709, places=6)

        # 10 % dearer: the standing still wins.
        self.assertEqual(
            self._order({"vouched": 1.0}, {}, {"vouched": _cost(1100), "plain": _cost(1000)})[0],
            "vouched",
        )
        # 11 % dearer, past the ceiling: price wins.
        self.assertEqual(
            self._order({"vouched": 1.0}, {}, {"vouched": _cost(1110), "plain": _cost(1000)})[0],
            "plain",
        )

    def test_an_operator_cannot_raise_the_weight_past_the_donation_weight(self):
        # The check is in the validator rather than here, but the pairing is the point:
        # the ceiling above is only a ceiling because the config refuses to lift it past
        # what a donation buys.
        from src.utils.config_validation import (
            ConfigValidationError,
            validate_balancers_config,
        )

        with self.assertRaises(ConfigValidationError):
            validate_balancers_config({"balancers": {
                "ONCHAIN_REPUTATION_WEIGHT": 2.0, "DONATION_WEIGHT": 0.3,
            }})


if __name__ == "__main__":
    unittest.main()
