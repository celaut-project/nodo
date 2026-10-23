"""The build and the first runtime come out of `initial_mu` (#563).

The caller pays `initial_mu` and nothing else. The node takes the cost of the build from
the balance of the instance that it starts, and the balance that is left must pay one
maintenance period. These tests cover the two rules, the launch on this node (the balance
that is stored, and the refund when the start does not fit), the amount that is sent to a
peer, and the meaning of the quote.

The imports are not guarded, for the reason given in tests/test_pricing_model.py: a
test of money that is skipped says that the sums were checked when they were not.
"""

import unittest
from unittest.mock import MagicMock, patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.gateway.launcher.local_execution import local_execution as local_execute
from src.payment_system.mu_conversion import MatchingPaymentSystem, configuration_for_peer
from src.utils.cost_functions import start_funding
from src.utils.cost_functions.delegated_price import covers, peer_build_mu
from src.utils.cost_functions.execution_cost import CPU, DISK, MEM, worst_case_charge_mu
from src.utils.cost_functions.start_funding import (
    StartAmountTooSmall,
    balance_after_build,
    minimum_runtime_mu,
    start_build_mu,
)
from tests.test_price_volatility import GIB, IDLE, _config, _guest

PERIOD_SECONDS = 10  # MANAGER_ITERATION_TIME of config.example.yaml.


class TheTwoRulesTests(unittest.TestCase):
    def test_the_balance_is_what_is_left_after_the_build(self):
        self.assertEqual(balance_after_build(1_000, 300, 500), 700)

    def test_the_least_amount_is_the_build_plus_one_period(self):
        self.assertEqual(balance_after_build(800, 300, 500), 500)

    def test_a_build_that_does_not_fit_is_rejected(self):
        with self.assertRaisesRegex(StartAmountTooSmall, "does not cover the build"):
            balance_after_build(299, 300, 0)

    def test_a_balance_under_one_period_is_rejected_and_says_the_least_amount(self):
        with self.assertRaises(StartAmountTooSmall) as caught:
            balance_after_build(799, 300, 500)
        self.assertIn("The least initial_mu is 800 MU", str(caught.exception))

    def test_a_service_that_is_already_built_costs_no_build(self):
        self.assertEqual(balance_after_build(500, 0, 500), 500)


class TheBuildTests(unittest.TestCase):
    def test_the_build_is_the_price_of_the_node(self):
        with _config(), patch.object(start_funding, "build_charge_mu", return_value=10_000_000):
            self.assertEqual(start_build_mu(celaut.Metadata(), scarcity=IDLE), 10_000_000)

    def test_a_node_that_is_free_takes_no_build(self):
        with _config(free_tier__FREE_WHILE_SCARCITY_BELOW=0.5), patch.object(
            start_funding, "build_charge_mu", return_value=10_000_000
        ):
            self.assertEqual(start_build_mu(celaut.Metadata(), scarcity=IDLE), 0)


class TheMinimumRuntimeTests(unittest.TestCase):
    def _resources(self):
        return celaut.Service.Container.Resources(at_init=_guest())

    def test_it_is_one_maintenance_period_with_the_resources_at_the_start(self):
        with _config(pricing__MAX_PRICE_VOLATILITY=0):
            expected = worst_case_charge_mu(_guest(), PERIOD_SECONDS, scarcity=IDLE)
            self.assertGreater(expected, 0)
            self.assertEqual(minimum_runtime_mu(self._resources(), scarcity=IDLE), expected)

    def test_with_the_bound_on_it_is_the_launch_price_times_the_bound(self):
        with _config(pricing__MAX_PRICE_VOLATILITY=2.0):
            at_launch = minimum_runtime_mu(self._resources(), scarcity=IDLE)
        with _config(pricing__MAX_PRICE_VOLATILITY=0):
            at_current_price = minimum_runtime_mu(self._resources(), scarcity=IDLE)
        # Each resource rounds on its own, so the two sides can differ by a few MU.
        self.assertAlmostEqual(at_launch, 2 * at_current_price, delta=5)

    def test_with_the_bound_off_it_is_the_price_of_the_current_load(self):
        loaded = {CPU: 0.5, MEM: 0.5, DISK: 0.5}
        with _config(pricing__MAX_PRICE_VOLATILITY=0):
            self.assertGreater(
                minimum_runtime_mu(self._resources(), scarcity=loaded),
                minimum_runtime_mu(self._resources(), scarcity=IDLE),
            )

    def test_it_is_the_figure_that_the_quote_gives_as_the_most_one_period_can_cost(self):
        # The same function prices `max_maintenance_cost` for the resources at the start.
        with _config():
            self.assertEqual(
                minimum_runtime_mu(self._resources(), scarcity=IDLE),
                worst_case_charge_mu(_guest(), PERIOD_SECONDS, scarcity=IDLE),
            )

    def test_a_service_with_no_resources_at_the_start_has_no_minimum(self):
        with _config():
            self.assertEqual(minimum_runtime_mu(celaut.Service.Container.Resources()), 0)


class TheLaunchOnThisNodeTests(unittest.TestCase):
    """`local_execution` stores `initial_mu - build`, and rejects a start that does not fit."""

    def _launch(self, *, initial_mu, build_mu, minimum_mu, build_error=None):
        values = {
            "network.ISOLATE_INTERNAL_CHILDREN": True,
            "network.CONSIDER_DEV_AS_INTERNAL": True,
            "network.DISABLE_EXPOSE_OUTSIDE": False,
            "network.FREE_PORTS_RANGE": [],
        }
        resolved = celaut.Sysresources(
            mem_limit=GIB, disk_space=GIB, cpu_period=100000, cpu_quota=100000
        )

        def execute(**kwargs):
            kwargs["register_instance"]("vm-1", "192.168.200.38", resolved)
            return ("vm-1", "192.168.200.38", resolved)

        config = celaut.Configuration()
        if initial_mu is not None:
            config.initial_mu.n = str(initial_mu)
        resources = celaut.Service.Container.Resources(at_init=_guest())
        service = celaut.Service(api=celaut.Service.Api(slot=[
            celaut.Service.Api.Slot(port=5000, transport=celaut.Service.Api.Protocol(tags=["tcp"]))
        ]))
        add = MagicMock(return_value=True)
        refunded = []
        with _config(**values), \
                patch.object(local_execute, "start_build_mu", return_value=build_mu), \
                patch.object(local_execute, "minimum_runtime_mu", return_value=minimum_mu), \
                patch.object(local_execute, "default_initial_balance", return_value=1_000), \
                patch.object(local_execute, "get_configured_virtualizer", return_value="ch"), \
                patch.object(local_execute, "select_virtualizer", return_value="ch"), \
                patch.object(local_execute, "build", return_value="svc-hash",
                             side_effect=build_error) as build, \
                patch.object(local_execute, "reserve_instance_name", return_value="tidy-island"), \
                patch.object(local_execute.sc, "internal_instance_exists", return_value=False), \
                patch.object(local_execute, "resolve_slot_transport_protocols", return_value="tcp"), \
                patch.object(local_execute, "get_free_port", return_value=51000), \
                patch.object(local_execute.sc, "add_local_instance", add), \
                patch.object(local_execute.sc, "set_local_instance_definition"), \
                patch.object(local_execute.sc, "mark_local_killed"), \
                patch.object(local_execute, "execute", side_effect=execute) as execute_mock:
            error = None
            try:
                local_execute.local_execution(
                    config=config, resources=resources,
                    father_id="dev-1", father_ip="127.0.0.1", metadata=celaut.Metadata(),
                    service=service, service_id="svc-hash",
                    refund_container=[lambda: refunded.append(True)],
                )
            except Exception as e:
                error = e
        return type("Launch", (), dict(
            error=error, add=add, refunded=refunded, build=build, execute=execute_mock
        ))

    def test_the_balance_of_the_instance_is_initial_mu_less_the_build(self):
        r = self._launch(initial_mu=1_500, build_mu=300, minimum_mu=500)

        self.assertIsNone(r.error)
        self.assertEqual(r.add.call_args.kwargs["balance_mu"], 1_200)
        self.assertEqual(r.refunded, [])

    def test_a_service_that_is_built_keeps_the_whole_amount(self):
        r = self._launch(initial_mu=1_500, build_mu=0, minimum_mu=500)

        self.assertEqual(r.add.call_args.kwargs["balance_mu"], 1_500)

    def test_the_least_amount_that_is_enough_starts(self):
        r = self._launch(initial_mu=800, build_mu=300, minimum_mu=500)

        self.assertIsNone(r.error)
        self.assertEqual(r.add.call_args.kwargs["balance_mu"], 500)

    def test_with_no_amount_the_balance_is_the_default_one(self):
        # The default balance is 1 000: the build is added to the amount that the father
        # would pay, and taken out of it again.
        r = self._launch(initial_mu=None, build_mu=300, minimum_mu=500)

        self.assertIsNone(r.error)
        self.assertEqual(r.add.call_args.kwargs["balance_mu"], 1_000)

    def test_a_build_that_does_not_fit_is_rejected_and_the_charge_goes_back(self):
        r = self._launch(initial_mu=200, build_mu=300, minimum_mu=0)

        self.assertIsInstance(r.error, StartAmountTooSmall)
        self.assertEqual(r.refunded, [True])
        r.build.assert_not_called()
        r.add.assert_not_called()
        r.execute.assert_not_called()

    def test_a_balance_under_one_period_is_rejected_and_the_charge_goes_back(self):
        r = self._launch(initial_mu=700, build_mu=300, minimum_mu=500)

        self.assertIsInstance(r.error, StartAmountTooSmall)
        self.assertEqual(r.refunded, [True])
        r.build.assert_not_called()

    def test_a_build_that_fails_gives_the_whole_charge_back(self):
        # The father never loses `initial_mu` on a failure.
        r = self._launch(initial_mu=1_500, build_mu=300, minimum_mu=500,
                         build_error=RuntimeError("the build failed"))

        self.assertIsInstance(r.error, RuntimeError)
        self.assertEqual(r.refunded, [True])
        r.add.assert_not_called()


class TheAmountForAPeerTests(unittest.TestCase):
    SYSTEM = MatchingPaymentSystem(
        ledger_tag="ergo", contract_hash="x",
        local_mu_per_unit=1_000_000_000, peer_mu_per_unit=3_000_000_000,
    )

    def test_the_configuration_for_a_peer_can_carry_a_smaller_amount(self):
        config = celaut.Configuration()
        config.initial_mu.n = "1000000"

        peer_config = configuration_for_peer(config, payment_system=self.SYSTEM, initial_mu=900_000)

        self.assertEqual(int(peer_config.initial_mu.n), 2_700_000)
        # The configuration of the father is not touched.
        self.assertEqual(int(config.initial_mu.n), 1_000_000)

    def test_an_amount_handed_to_a_peer_rounds_down(self):
        system = MatchingPaymentSystem(
            ledger_tag="ergo", contract_hash="x",
            local_mu_per_unit=3, peer_mu_per_unit=1,
        )
        config = celaut.Configuration()
        config.initial_mu.n = "10"

        self.assertEqual(
            int(configuration_for_peer(config, payment_system=system, initial_mu=10).initial_mu.n), 3
        )

    def test_with_no_override_the_amount_is_the_one_of_the_configuration(self):
        config = celaut.Configuration()
        config.initial_mu.n = "1000000"

        peer_config = configuration_for_peer(config, payment_system=self.SYSTEM)

        self.assertEqual(int(peer_config.initial_mu.n), 3_000_000)


class TheQuoteTests(unittest.TestCase):
    """`EstimatedCost.cost` keeps its value: it is the build plus the balance asked for."""

    def test_the_quote_of_the_node_is_the_build_plus_the_balance(self):
        from src.utils.cost_functions import general_cost_functions

        with _config(), patch.object(general_cost_functions, "build_charge_mu", return_value=300), \
                patch.object(general_cost_functions, "is_free", return_value=False):
            self.assertEqual(
                general_cost_functions.compute_start_service_cost(celaut.Metadata(), 1_000), 1_300
            )

    def test_the_build_of_the_peer_is_what_its_quote_has_above_the_balance(self):
        peer_cost = celaut.EstimatedCost(cost=celaut.Amount(n="1200"))

        self.assertEqual(peer_build_mu(peer_cost, 1_000), 200)
        # A quote below the balance has no build.
        self.assertEqual(peer_build_mu(peer_cost, 5_000), 0)

    def test_a_peer_is_covered_when_our_price_is_not_below_its_quote(self):
        # The start and one period (#577): what this node charges covers what the peer
        # would charge it, and a peer that quotes more is not covered.
        def quote(cost, period):
            return celaut.EstimatedCost(
                cost=celaut.Amount(n=str(cost)), init_maintenance_cost=celaut.Amount(n=str(period)))

        own = quote(1_300, 10)
        self.assertTrue(covers(own, quote(1_200, 10)))
        self.assertTrue(covers(own, quote(1_300, 5)))
        self.assertFalse(covers(own, quote(1_301, 5)))
        self.assertFalse(covers(own, quote(1_200, 11)))
        self.assertTrue(covers(own, celaut.EstimatedCost()))

    def test_a_peer_is_not_covered_when_the_service_may_grow_into_a_loss(self):
        # The service changes its resources on the peer (#577): one period at the most
        # that it may grow to must be covered too.
        own = celaut.EstimatedCost(cost=celaut.Amount(n="1300"),
                                   max_maintenance_cost=celaut.Amount(n="40"))
        self.assertTrue(covers(own, celaut.EstimatedCost(max_maintenance_cost=celaut.Amount(n="40"))))
        self.assertFalse(covers(own, celaut.EstimatedCost(max_maintenance_cost=celaut.Amount(n="41"))))



class DelegatedPriceTests(unittest.TestCase):
    """The price of this node for an instance that a peer runs (#577)."""

    def _own(self, resources, scarcity=IDLE, **config):
        from src.utils.cost_functions import delegated_price

        with _config(**config), \
                patch.object(delegated_price, "system_scarcity", return_value=scarcity), \
                patch.object(delegated_price, "build_charge_mu", return_value=300), \
                patch.object(delegated_price, "resolve_billable_resources",
                             side_effect=lambda shape, *_, **__: shape):
            return delegated_price.own_price(
                celaut.Metadata(), celaut.Configuration(initial_mu=celaut.Amount(n="1000")),
                resources)

    def test_a_delegated_instance_is_not_free_while_this_node_is(self):
        resources = celaut.Service.Container.Resources(at_init=_guest())
        own = self._own(resources, free_tier__FREE_WHILE_SCARCITY_BELOW=0.5)

        # The build and the period are charged although this node is idle and free.
        self.assertEqual(int(own.cost.n), 1_300)
        self.assertGreater(int(own.init_maintenance_cost.n), 0)

    def test_the_period_is_priced_at_the_multiplier_of_the_launch(self):
        resources = celaut.Service.Container.Resources(at_init=_guest(), at_most=_guest())
        idle = self._own(resources, IDLE)
        full = self._own(resources, {CPU: 1.0, MEM: 1.0, DISK: 1.0})

        self.assertGreater(int(full.init_maintenance_cost.n), int(idle.init_maintenance_cost.n))
        self.assertEqual(full.init_maintenance_cost.n, full.max_maintenance_cost.n)

    def test_a_manifest_with_no_start_is_charged_the_floors(self):
        from src.utils.cost_functions import delegated_price

        floors = celaut.Sysresources(mem_limit=GIB, cpu_period=100000, cpu_quota=100000)
        with patch.object(delegated_price, "resolve_billable_resources", return_value=floors):
            start, most = delegated_price.billable_shapes(celaut.Service.Container.Resources())
        self.assertEqual((start.mem_limit, most.mem_limit), (GIB, GIB))

    def test_an_architecture_this_node_does_not_have_is_charged_the_build(self):
        from src.utils.cost_functions import delegated_price
        from src.virtualizers.architecture import UnsupportedArchitectureException

        with _config(), patch.object(delegated_price, "build_charge_mu",
                                     side_effect=UnsupportedArchitectureException(arch="x")):
            self.assertEqual(delegated_price.own_build_mu(celaut.Metadata()), 10_000_000)

    def test_a_period_is_compared_per_second(self):
        # This node prices 60 s, and the peer 10 s: 60 MU against 11 MU is a loss.
        own = celaut.EstimatedCost(init_maintenance_cost=celaut.Amount(n="60"),
                                   maintenance_seconds_loop=60)
        dear = celaut.EstimatedCost(init_maintenance_cost=celaut.Amount(n="11"),
                                    maintenance_seconds_loop=10)
        cheap = celaut.EstimatedCost(init_maintenance_cost=celaut.Amount(n="10"),
                                     maintenance_seconds_loop=10)
        self.assertFalse(covers(own, dear))
        self.assertTrue(covers(own, cheap))


if __name__ == "__main__":
    unittest.main()
