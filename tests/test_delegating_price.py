"""A node that only delegates sets its price from its peers (#684).

With ``network.EXECUTE_LOCALLY: false`` the price of this node (``own_price``) has no
link to what a peer charges it. What the tests protect:

* for each figure, the price is the higher of ``own_price`` and the median of the
  quotes of the peers: with one peer, an odd and an even number of peers, and none;
* the maintenance of a peer is put in the period of this node first;
* no figure is lower than that of the cheapest peer, and at least one peer is covered;
* the balancer keeps a peer that quotes more than ``own_price``, and the launcher gets
  the raised price; with ``network.EXECUTE_LOCALLY: true`` nothing changes.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.balancers.execution_balancer import execution_balancer as balancer_mod
    from src.utils.cost_functions.delegated_price import covers, delegating_price
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def price(cost, init=0, maximum=0, seconds=60):
    return celaut.EstimatedCost(
        cost=celaut.Amount(n=str(cost)),
        init_maintenance_cost=celaut.Amount(n=str(init)),
        max_maintenance_cost=celaut.Amount(n=str(maximum)),
        maintenance_seconds_loop=seconds,
        price_bounded=True,
    )


def figures(estimated):
    return (int(estimated.cost.n), int(estimated.init_maintenance_cost.n),
            int(estimated.max_maintenance_cost.n))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegatingPriceTests(unittest.TestCase):
    OWN = price(100, 10, 20)

    def test_with_no_peer_the_price_is_the_own_price(self):
        self.assertEqual(delegating_price(self.OWN, []), self.OWN)

    def test_with_one_peer_the_price_is_its_quote_where_it_is_higher(self):
        quoted = delegating_price(self.OWN, [price(300, 5, 40)])
        self.assertEqual(figures(quoted), (300, 10, 40))

    def test_with_an_odd_number_of_peers_the_price_is_the_middle_quote(self):
        quoted = delegating_price(self.OWN, [
            price(200, 30, 60), price(400, 50, 80), price(300, 40, 70)])
        self.assertEqual(figures(quoted), (300, 40, 70))

    def test_with_an_even_number_of_peers_the_price_is_the_mean_of_the_middle_quotes(self):
        quoted = delegating_price(self.OWN, [
            price(200, 30, 60), price(301, 41, 71), price(500, 50, 90), price(900, 99, 99)])
        # Rounded up: (301 + 500) / 2 = 400.5.
        self.assertEqual(figures(quoted), (401, 46, 81))

    def test_the_own_price_is_kept_where_it_is_higher(self):
        quoted = delegating_price(price(1_000, 100, 200), [price(300, 40, 70)])
        self.assertEqual(figures(quoted), (1_000, 100, 200))

    def test_the_other_fields_are_those_of_the_own_price(self):
        own = price(100, 10, 20, seconds=60)
        own.price_volatility = 0.0
        quoted = delegating_price(own, [price(300, 40, 70, seconds=30)])
        self.assertEqual(quoted.maintenance_seconds_loop, 60)
        self.assertTrue(quoted.price_bounded)
        self.assertEqual(quoted.price_volatility, 0.0)

    def test_the_maintenance_of_a_peer_is_put_in_the_period_of_this_node(self):
        # 40 MU per 30 s is 80 MU per 60 s.
        quoted = delegating_price(price(100, 10, 20, seconds=60), [price(100, 40, 41, seconds=30)])
        self.assertEqual(figures(quoted), (100, 80, 82))

    def test_no_figure_is_lower_than_that_of_the_cheapest_peer(self):
        peers = [price(500, 3, 9), price(200, 50, 60), price(350, 20, 30)]
        quoted = delegating_price(self.OWN, peers)
        for index in range(3):
            self.assertGreaterEqual(figures(quoted)[index],
                                    min(figures(peer)[index] for peer in peers))

    def test_a_peer_is_covered_also_when_no_peer_is_below_every_median(self):
        # The medians are (5, 5, 5): each peer is above one of them.
        peers = [price(1, 9, 9), price(9, 1, 1)]
        quoted = delegating_price(price(0), peers)
        self.assertTrue(any(covers(quoted, peer) for peer in peers))
        # The peer that needs the smallest raise is the one covered.
        self.assertEqual(figures(quoted), (9, 5, 5))

    def test_the_own_price_is_not_changed(self):
        own = price(100, 10, 20)
        delegating_price(own, [price(300, 40, 70)])
        self.assertEqual(figures(own), (100, 10, 20))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BalancerTests(unittest.TestCase):
    def _run(self, execute_locally, quotes, own):
        settings = {
            "network.EXECUTE_LOCALLY": execute_locally,
            "network.DELEGATE_EXECUTION": True,
        }
        with mock.patch.object(balancer_mod.env_manager, "get",
                               side_effect=lambda key, default=None: settings.get(key, default)), \
                mock.patch.object(balancer_mod, "generate_estimated_cost", return_value=price(100, 10, 20)), \
                mock.patch.object(balancer_mod.Registry, "can_forward", return_value=True), \
                mock.patch.object(balancer_mod, "quotes_from_peers", return_value=quotes), \
                mock.patch.object(balancer_mod, "estimated_cost_sorter",
                                  side_effect=lambda estimated_costs, **_: iter(estimated_costs.items())):
            return [peer for peer, _ in balancer_mod.execution_balancer(
                service_id="s1",
                resources=celaut.Service.Container.Resources(),
                metadata=celaut.Metadata(),
                configuration=celaut.Configuration(),
                own=own,
            )]

    def test_a_node_that_only_delegates_keeps_a_peer_dearer_than_its_own_price(self):
        own = price(100, 10, 20)
        candidates = self._run(False, {"peer-1": price(200, 20, 40)}, own)
        self.assertEqual(candidates, ["peer-1"])
        # The launcher charges the figure that the peer was compared with.
        self.assertEqual(figures(own), (200, 20, 40))

    def test_a_node_that_only_delegates_drops_a_peer_above_the_median(self):
        own = price(100, 10, 20)
        candidates = self._run(False, {
            "peer-1": price(200, 20, 40), "peer-2": price(300, 30, 50), "peer-3": price(900, 90, 90),
        }, own)
        self.assertEqual(sorted(candidates), ["peer-1", "peer-2"])
        self.assertEqual(figures(own), (300, 30, 50))

    def test_with_no_peer_the_own_price_does_not_change(self):
        own = price(100, 10, 20)
        self.assertEqual(self._run(False, {}, own), [])
        self.assertEqual(figures(own), (100, 10, 20))

    def test_a_node_that_runs_services_still_drops_a_dearer_peer(self):
        own = price(100, 10, 20)
        candidates = self._run(True, {"peer-1": price(200, 20, 40)}, own)
        self.assertEqual(candidates, ["local"])
        self.assertEqual(figures(own), (100, 10, 20))


if __name__ == "__main__":
    unittest.main()
