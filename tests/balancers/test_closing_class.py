"""The interruption class of a candidate, and where it sits in the sort (issue #344, phase 3)."""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.balancers.estimated_cost_sorter import estimated_cost_sorter as sorter
    from src.balancers.estimated_cost_sorter import interruption
    from src.utils.cost_functions import funded_life
    from src.utils.utils import to_amount
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    sorter = None  # type: ignore[assignment]

NOW = 1_800_000_000
LOOP = 10          # seconds in one maintenance period
INIT = 100         # MU for one period
FUNDED = 360       # periods: one hour of funded life at LOOP = 10


def _cost(*, on_close=0, closes_at=0, build=0, bounded=True, volatility=0.0, price=1000):
    return celaut_pb2.EstimatedCost(
        cost=to_amount(build),
        init_maintenance_cost=to_amount(INIT),
        max_maintenance_cost=to_amount(INIT),
        maintenance_seconds_loop=LOOP,
        price_bounded=bounded,
        price_volatility=volatility,
        on_close=on_close,
        closes_at=closes_at,
    )


def _class(quote, initial_mu=INIT * FUNDED):
    return interruption.interruption_class(quote, initial_mu, now=NOW)


LIFE = FUNDED * LOOP  # seconds
INSIDE = NOW + LIFE - 1
OUTSIDE = NOW + LIFE


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FiveClassesTests(unittest.TestCase):
    def test_the_five_classes_in_order(self):
        refuse = _class(_cost(on_close=celaut_pb2.ON_CLOSE_REFUSE, closes_at=INSIDE))
        hib_out = _class(_cost(on_close=celaut_pb2.ON_CLOSE_HIBERNATE, closes_at=OUTSIDE))
        stop_out = _class(_cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=OUTSIDE))
        hib_in = _class(_cost(on_close=celaut_pb2.ON_CLOSE_HIBERNATE, closes_at=INSIDE))
        stop_in = _class(_cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=INSIDE))
        self.assertEqual([refuse, hib_out, stop_out, hib_in, stop_in], [0, 1, 2, 3, 4])

    def test_no_close_and_refuse_are_class_zero(self):
        self.assertEqual(_class(_cost()), 0)
        self.assertEqual(_class(_cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=0)), 0)
        self.assertEqual(_class(_cost(on_close=celaut_pb2.ON_CLOSE_REFUSE, closes_at=NOW + 5)), 0)

    def test_a_close_after_the_funded_life_moves_a_stop_from_4_to_2_and_a_hibernation_from_3_to_1(self):
        stop = _cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=OUTSIDE)
        hibernate = _cost(on_close=celaut_pb2.ON_CLOSE_HIBERNATE, closes_at=OUTSIDE)
        # With the balance of the test the close is at the end of the funded life: outside.
        self.assertEqual((_class(stop), _class(hibernate)), (2, 1))
        # With twice the balance the life is twice as long, and the same close is inside it.
        self.assertEqual((_class(stop, 2 * INIT * FUNDED), _class(hibernate, 2 * INIT * FUNDED)), (4, 3))

    def test_a_stop_is_always_worse_than_a_hibernation_in_the_same_position(self):
        for closes_at in (INSIDE, OUTSIDE):
            hibernate = _class(_cost(on_close=celaut_pb2.ON_CLOSE_HIBERNATE, closes_at=closes_at))
            stop = _class(_cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=closes_at))
            self.assertLess(hibernate, stop)

    def test_the_distance_of_the_close_is_not_used_inside_a_class(self):
        near = _class(_cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=NOW + 1))
        far = _class(_cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=INSIDE))
        self.assertEqual(near, far)

    def test_a_request_with_no_initial_mu_uses_the_default_periods(self):
        quote = _cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=NOW + 5 * LOOP)
        with mock.patch.object(funded_life, "default_initial_periods", return_value=100):
            self.assertEqual(interruption.interruption_class(quote, None, now=NOW), 4)
        quote = _cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=NOW + 100 * LOOP)
        with mock.patch.object(funded_life, "default_initial_periods", return_value=100):
            self.assertEqual(interruption.interruption_class(quote, None, now=NOW), 2)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FundedLifeTests(unittest.TestCase):
    def test_the_builds_come_out_of_the_balance(self):
        quote = _cost(build=INIT * 100)
        self.assertEqual(funded_life.funded_life_seconds(quote, INIT * 300), 200 * LOOP)

    def test_a_balance_that_does_not_pay_a_period_has_no_life(self):
        self.assertEqual(funded_life.funded_life_seconds(_cost(), INIT - 1), 0)
        self.assertEqual(funded_life.funded_life_seconds(_cost(build=INIT * 5), INIT * 5), 0)

    def test_a_price_bound_shortens_the_funded_life(self):
        plain = funded_life.funded_life_seconds(_cost(), INIT * 100)
        bounded = funded_life.funded_life_seconds(_cost(volatility=0.5), INIT * 100)
        self.assertEqual(plain, 100 * LOOP)
        # Period 1 to N charge init * 1.5 each while the volatility period is 0: 66 pay.
        self.assertEqual(bounded, 66 * LOOP)

    def test_the_life_is_capped_at_the_horizon(self):
        self.assertEqual(funded_life.funded_life_seconds(_cost(), 10 ** 15),
                         funded_life.HORIZON_SECONDS)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheSortTests(unittest.TestCase):
    def _order(self, candidates, initial_mu=INIT * FUNDED, **kwargs):
        with mock.patch.object(sorter, "bonus_by_peer", return_value={}), \
                mock.patch.object(sorter, "standing_by_peer", return_value={}), \
                mock.patch.object(sorter, "compute_reputation", return_value=0.0):
            return [peer_id for peer_id, _ in sorter.estimated_cost_sorter(
                candidates, initial_mu=initial_mu, now=NOW, **kwargs)]

    def test_a_better_class_beats_a_better_price(self):
        candidates = {
            "cheap-but-stops": _cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=INSIDE, price=1),
            "dear-and-stays": _cost(build=500),
        }
        self.assertEqual(self._order(candidates), ["dear-and-stays", "cheap-but-stops"])

    def test_inside_a_class_the_order_is_the_order_of_the_score(self):
        candidates = {"dear": _cost(build=900), "cheap": _cost(build=10)}
        self.assertEqual(self._order(candidates), ["cheap", "dear"])

    def test_no_candidate_is_removed(self):
        candidates = {
            "a": _cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=INSIDE),
            "b": _cost(on_close=celaut_pb2.ON_CLOSE_HIBERNATE, closes_at=OUTSIDE),
            "c": _cost(),
        }
        self.assertEqual(sorted(self._order(candidates)), ["a", "b", "c"])

    def test_the_price_bound_class_still_comes_first(self):
        candidates = {
            "unbounded-stays": _cost(bounded=False),
            "bounded-stops": _cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=INSIDE),
        }
        with mock.patch.object(sorter, "prices") as prices:
            prices.return_value.price_bounded = True
            self.assertEqual(self._order(candidates), ["bounded-stops", "unbounded-stays"])

    def test_the_whole_order_across_classes(self):
        candidates = {
            "stop-in": _cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=INSIDE),
            "hib-in": _cost(on_close=celaut_pb2.ON_CLOSE_HIBERNATE, closes_at=INSIDE),
            "stop-out": _cost(on_close=celaut_pb2.ON_CLOSE_STOP, closes_at=OUTSIDE),
            "hib-out": _cost(on_close=celaut_pb2.ON_CLOSE_HIBERNATE, closes_at=OUTSIDE),
            "none": _cost(),
        }
        self.assertEqual(self._order(candidates),
                         ["none", "hib-out", "stop-out", "hib-in", "stop-in"])


if __name__ == "__main__":
    unittest.main()
