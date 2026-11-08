"""A service is funded for the descendants that it declares.

``Service.possible_environment_workload`` lists scenarios of descendants that the service can
start at the same time, and the service pays for them out of its own balance: each start takes
its ``initial_mu`` from the father. So the balance that a start funds it with
(``manager.default_initial_balance``) covers the dearest scenario, for the same hours as the
service itself, and one build for each group. The quote (``GetServiceEstimatedCost``), the
funding of the instance and the amount asked of a peer all go through that function, so they
give one figure.

The imports are not guarded, for the reason given in tests/test_pricing_model.py: a test of
money that is skipped says that the sums were checked when they were not.
"""

import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.manager.manager import default_initial_balance
from src.utils.cost_functions import start_funding
from src.utils.cost_functions.execution_cost import worst_case_charge_mu
from src.utils.cost_functions.start_funding import descendant_funding_mu
from src.virtualizers.interface import resolve_billable_resources
from tests.test_price_volatility import GIB, IDLE, _config, _guest

BUILD = 10_000_000  # pricing.BUILD_MU of the config of these tests.
HOURS = 3600
HASH = bytes(range(32))


def _resources(mem=GIB, disk=GIB) -> celaut.Sysresources:
    return celaut.Sysresources(mem_limit=mem, cpu_period=100000, cpu_quota=100000, disk_space=disk)


def _group(count=1, resources=None, hash_value=None):
    group = celaut.Service.PossibleEnvironmentWorkload.Workload(
        count=count, resources=resources or _resources()
    )
    if hash_value:
        group.dependency.hash.add(type=b"\x01" * 32, value=hash_value)
    return group


def _service(*scenarios) -> celaut.Service:
    service = celaut.Service()
    for groups in scenarios:
        service.possible_environment_workload.add().workloads.extend(groups)
    return service


def _runtime(resources, hours=1.0) -> int:
    """What one instance with `resources` costs for `hours`, at the worst case of the price."""
    return worst_case_charge_mu(
        system_resources=resolve_billable_resources(resources, None),
        seconds=hours * HOURS,
        scarcity=IDLE,
    )


class WhatADescendantCostsTests(unittest.TestCase):
    def test_a_service_with_no_workload_adds_nothing(self):
        with _config():
            self.assertEqual(descendant_funding_mu(celaut.Service(), scarcity=IDLE), 0)

    def test_a_group_costs_its_runtime_and_one_build(self):
        with _config():
            got = descendant_funding_mu(_service([_group()]), scarcity=IDLE)
            self.assertEqual(got, _runtime(_resources()) + BUILD)
            self.assertGreater(_runtime(_resources()), 0)

    def test_identical_instances_pay_their_runtime_each_and_one_build(self):
        with _config():
            got = descendant_funding_mu(_service([_group(count=3)]), scarcity=IDLE)
            self.assertEqual(got, 3 * _runtime(_resources()) + BUILD)

    def test_a_scenario_adds_up_its_groups_and_each_group_has_its_build(self):
        small, big = _resources(mem=GIB // 4), _resources(mem=2 * GIB)
        with _config():
            got = descendant_funding_mu(_service([_group(resources=small), _group(resources=big)]),
                                        scarcity=IDLE)
            self.assertEqual(got, _runtime(small) + _runtime(big) + 2 * BUILD)

    def test_the_scenarios_do_not_add_up_the_dearest_one_counts(self):
        cheap = [_group(resources=_resources(mem=GIB // 4))]
        dear = [_group(count=2, resources=_resources(mem=2 * GIB))]
        with _config():
            one = descendant_funding_mu(_service(dear), scarcity=IDLE)
            both = descendant_funding_mu(_service(cheap, dear), scarcity=IDLE)
            swapped = descendant_funding_mu(_service(dear, cheap), scarcity=IDLE)
            self.assertEqual(both, one)
            self.assertEqual(swapped, one)
            self.assertGreater(one, descendant_funding_mu(_service(cheap), scarcity=IDLE))

    def test_a_group_with_no_instances_costs_nothing(self):
        with _config():
            self.assertEqual(descendant_funding_mu(_service([_group(count=0)]), scarcity=IDLE), 0)

    def test_a_service_that_is_built_here_costs_no_build(self):
        with _config(), patch.object(start_funding, "is_built", return_value=True) as built:
            got = descendant_funding_mu(_service([_group(hash_value=HASH)]), scarcity=IDLE)
        built.assert_called_once_with(HASH.hex())
        self.assertEqual(got, _runtime(_resources()))

    def test_a_service_that_is_not_built_pays_its_build(self):
        with _config(), patch.object(start_funding, "is_built", return_value=False):
            got = descendant_funding_mu(_service([_group(hash_value=HASH)]), scarcity=IDLE)
        self.assertEqual(got, _runtime(_resources()) + BUILD)

    def test_a_group_with_resources_only_always_pays_its_build(self):
        with _config(), patch.object(start_funding, "is_built", return_value=True) as built:
            got = descendant_funding_mu(_service([_group()]), scarcity=IDLE)
        built.assert_not_called()
        self.assertEqual(got, _runtime(_resources()) + BUILD)

    def test_the_hours_are_the_ones_of_the_service(self):
        with _config(deposits__INITIAL_RUNTIME_HOURS=2.0):
            with patch.object(start_funding.env_manager, "get",
                              side_effect=lambda k, d=None: 2.0 if k == "deposits.INITIAL_RUNTIME_HOURS" else d):
                got = descendant_funding_mu(_service([_group()]), scarcity=IDLE)
            self.assertEqual(got, _runtime(_resources(), hours=2.0) + BUILD)
            self.assertGreater(got, _runtime(_resources()) + BUILD)

    def test_no_hours_no_funding(self):
        with patch.object(start_funding.env_manager, "get",
                          side_effect=lambda k, d=None: 0 if k == "deposits.INITIAL_RUNTIME_HOURS" else d):
            self.assertEqual(descendant_funding_mu(_service([_group()]), scarcity=IDLE), 0)

    def test_a_node_that_is_free_funds_nothing(self):
        with _config(), patch.object(start_funding, "is_free", return_value=True), \
                patch.object(start_funding, "worst_case_charge_mu", return_value=0):
            self.assertEqual(descendant_funding_mu(_service([_group()]), scarcity=IDLE), 0)


class TheDefaultBalanceTests(unittest.TestCase):
    """`default_initial_balance` is what the quote, the instance and the peer quote use."""

    def test_without_the_manifest_it_is_the_balance_of_the_service_alone(self):
        with _config():
            own = default_initial_balance(system_resources=_guest(), scarcity=IDLE)
            self.assertEqual(own, _runtime(_guest()))

    def test_a_service_with_no_workload_has_the_same_balance_with_or_without_the_manifest(self):
        with _config():
            alone = default_initial_balance(system_resources=_guest(), scarcity=IDLE)
            with_manifest = default_initial_balance(
                system_resources=_guest(), scarcity=IDLE, service=celaut.Service()
            )
            self.assertEqual(with_manifest, alone)

    def test_the_declared_workload_is_added_to_the_balance_of_the_service(self):
        service = _service([_group(count=2)])
        with _config():
            alone = default_initial_balance(system_resources=_guest(), scarcity=IDLE)
            funded = default_initial_balance(system_resources=_guest(), scarcity=IDLE, service=service)
            self.assertEqual(funded - alone, descendant_funding_mu(service, scarcity=IDLE))
            self.assertEqual(funded - alone, 2 * _runtime(_resources()) + BUILD)

    def test_a_service_with_no_resources_at_the_start_is_still_not_funded(self):
        with _config():
            self.assertEqual(
                default_initial_balance(system_resources=None, service=_service([_group()])), 0
            )


class TheCallersTests(unittest.TestCase):
    """The quote, the launch and the quote for a peer pass the manifest."""

    def test_every_caller_passes_the_service(self):
        import inspect

        from src.gateway.iterables import estimated_cost_iterable
        from src.gateway.launcher import launch_service
        from src.gateway.launcher.local_execution import local_execution

        for module in (estimated_cost_iterable, launch_service, local_execution):
            source = inspect.getsource(module)
            calls = source.count("default_initial_balance(")
            with_service = source.count("service=")
            self.assertGreaterEqual(calls, 1, module.__name__)
            # Each call in the source names its manifest: a caller that forgot it would
            # quote less than the launch funds.
            body = source.split("default_initial_balance(")[1:]
            for call in body:
                head = call.split(")\n", 1)[0]
                if "def default_initial_balance" in call[:40]:
                    continue
                self.assertIn("service=", head, f"{module.__name__}: {head[:80]}")
            self.assertGreaterEqual(with_service, calls, module.__name__)


if __name__ == "__main__":
    unittest.main()
