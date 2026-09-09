"""The balancer has to find its own donation credit, and a peer's, under the right key.

Two naming conventions meet in the sorter and the join between them is silent when it
is wrong: the execution balancer calls this node ``'local'``, while the donation index
is keyed as ``contract_instance.peer_id`` is -- ``LOCAL``. Looking our own credit up by
the balancer's name returns nothing, and nothing is indistinguishable from a node that
has never donated. §7.1 of the issue requires the opposite: our donations are read off
the chain and counted with the same list as everybody else's.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.balancers.estimated_cost_sorter import estimated_cost_sorter as sorter
    from src.database.sql_connection import LOCAL_PEER_ID
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
class DonationLookupTests(unittest.TestCase):

    def _order(self, bonuses, candidates):
        with mock.patch.object(sorter, "bonus_by_peer", return_value=bonuses), \
                mock.patch.object(sorter, "compute_reputation", return_value=0.0):
            return [peer_id for peer_id, _ in sorter.estimated_cost_sorter(candidates)]

    def test_our_own_credit_is_found_under_the_catalogues_name_for_us(self):
        """Priced where only our own credit can decide.

        At 3x a peer's price, LOCAL_BIAS alone (1.0, i.e. e**1 = 2.72x) loses. Add a
        full donation bonus and the tolerance becomes e**1.3 = 3.67x, so local wins --
        but only if its credit was found. Looked up by the balancer's own name for
        itself it reads as zero, and zero is what a node that never donated reads as.
        """
        candidates = {"local": _cost(3000), "peer-a": _cost(1000)}

        self.assertEqual(self._order({}, candidates)[0], "peer-a")
        self.assertEqual(
            self._order({LOCAL_PEER_ID: 1.0}, candidates)[0],
            "local",
            "our own donations must credit us",
        )

    def test_a_peer_is_found_under_its_own_id(self):
        # Between two peers, so the comparison is not against LOCAL_BIAS -- which at
        # 1.0 outweighs the whole donation term, and rightly: preferring to run work
        # here is a stronger policy than a tie-break.
        candidates = {"peer-a": _cost(1000), "peer-b": _cost(1000)}
        self.assertEqual(self._order({"peer-a": 1.0}, candidates)[0], "peer-a")

    def test_the_donation_term_never_outweighs_the_home_field_preference(self):
        # Worth pinning: at the shipped defaults a donating peer does not pull work off
        # this node at the same price. W_d (0.3) is a tie-break; LOCAL_BIAS (1.0) is a
        # policy, and the issue changes neither.
        candidates = {"local": _cost(1000), "peer-a": _cost(1000)}
        self.assertEqual(self._order({"peer-a": 1.0}, candidates)[0], "local")

    def test_no_credit_anywhere_leaves_the_existing_policy_untouched(self):
        """LOCAL_BIAS decides, exactly as it did before donations existed.

        Local tolerates a price up to e**1 (~2.7x) higher than a peer's; at 3x it
        delegates. This is the regression guard on today's delegation behaviour.
        """
        self.assertEqual(
            self._order({}, {"local": _cost(2500), "peer-a": _cost(1000)})[0], "local"
        )
        self.assertEqual(
            self._order({}, {"local": _cost(3000), "peer-a": _cost(1000)})[0], "peer-a"
        )


if __name__ == "__main__":
    unittest.main()
