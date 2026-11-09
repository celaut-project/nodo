"""The quote has the cost of the initial children of a service (#538, absorbs #628).

``EstimatedCost.cost`` includes what the parent pays for the ``child_start`` of the
children of its ``initial_environment_workload``. For one workload it is ``count`` times
the build of the child plus the default balance of this node for the ``at_init`` of the
child. A group adds up its workloads, a clause takes its dearest group, and the clauses add
up. ``possible_environment_workload`` has no cost. There is no separate field.

The imports are not guarded, for the reason given in tests/test_pricing_model.py: a test of
money that is skipped says that the sums were checked when they were not.
"""

import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.manager.manager import default_initial_balance  # noqa: E402
from src.utils.cost_functions import start_funding  # noqa: E402
from src.utils.cost_functions.start_funding import (  # noqa: E402
    child_start_cost_mu,
    initial_children_mu,
)
from src.utils.utils import from_amount  # noqa: E402
from src.utils.verify import get_service_hex_main_hash, get_service_list_of_hashes  # noqa: E402
from tests.test_price_volatility import GIB, IDLE, _config  # noqa: E402

BUILD = 10_000_000  # pricing.BUILD_MU of the config of these tests.
HASH = bytes(range(32))


def _at_init(mem=GIB, disk=GIB) -> celaut.Sysresources:
    return celaut.Sysresources(mem_limit=mem, cpu_period=100000, cpu_quota=100000, disk_space=disk)


def _workload(count=1, mem=GIB, hash_value=None, resources=True) -> celaut.Service.Workload:
    workload = celaut.Service.Workload(count=count)
    if resources:
        workload.dependency.service.container.resources.at_init.CopyFrom(_at_init(mem=mem))
    if hash_value:
        workload.dependency.hash.add(type=b"\x01" * 32, value=hash_value)
    return workload


def _service(*clauses, field="initial_environment_workload") -> celaut.Service:
    """Each clause is a list of groups, each group a list of workloads."""
    service = celaut.Service()
    for groups in clauses:
        clause = getattr(service, field).add()
        for workloads in groups:
            clause.any_of.add().workloads.extend(workloads)
    return service


def _balance(mem=GIB) -> int:
    """The default balance of one child with ``_at_init(mem)``, at an idle node."""
    return default_initial_balance(system_resources=_at_init(mem=mem), scarcity=IDLE)


class OneWorkloadTests(unittest.TestCase):
    def test_a_workload_with_no_service_named_costs_its_balance_and_no_build(self):
        with _config():
            got = child_start_cost_mu(_workload(), scarcity=IDLE)
            self.assertEqual(got, _balance())
            self.assertGreater(got, 0)

    def test_count_multiplies_the_start(self):
        with _config(), patch.object(start_funding, "is_built", return_value=False):
            got = child_start_cost_mu(_workload(count=3, hash_value=HASH), scarcity=IDLE)
            self.assertEqual(got, 3 * (BUILD + _balance()))

    def test_a_service_that_is_built_here_costs_no_build(self):
        with _config(), patch.object(start_funding, "is_built", return_value=True) as built:
            got = child_start_cost_mu(_workload(hash_value=HASH), scarcity=IDLE)
            self.assertEqual(got, _balance())
        built.assert_called_once_with(HASH.hex())

    def test_a_complete_service_with_no_hash_is_named_by_its_own_hash(self):
        workload = _workload()
        workload.dependency.is_completed = True
        expected = get_service_hex_main_hash(other_hashes=get_service_list_of_hashes(
            workload.dependency.service.SerializeToString()))
        with _config(), patch.object(start_funding, "is_built", return_value=False) as built:
            got = child_start_cost_mu(workload, scarcity=IDLE)
            self.assertEqual(got, BUILD + _balance())
        built.assert_called_once_with(expected)

    def test_a_workload_with_no_resources_is_priced_at_the_floors(self):
        with _config():
            got = child_start_cost_mu(_workload(resources=False), scarcity=IDLE)
            floors = default_initial_balance(system_resources=celaut.Sysresources(), scarcity=IDLE)
        self.assertEqual(got, floors)
        self.assertGreater(got, 0)

    def test_count_zero_costs_nothing(self):
        with _config():
            self.assertEqual(child_start_cost_mu(_workload(count=0), scarcity=IDLE), 0)

    def test_a_node_that_is_free_charges_the_balance_and_no_build(self):
        # #636, H: a child started while the node is free gets a balance too.
        with _config(), patch.object(start_funding, "is_free", return_value=True), \
                patch.object(start_funding, "is_built", return_value=False):
            got = child_start_cost_mu(_workload(count=2, hash_value=HASH), scarcity=IDLE)
            self.assertEqual(got, 2 * _balance())
            self.assertGreater(got, 0)


class TheDeclarationTests(unittest.TestCase):
    def test_a_service_with_no_initial_workload_costs_nothing(self):
        with _config():
            self.assertEqual(initial_children_mu(celaut.Service(), scarcity=IDLE), 0)
            self.assertEqual(initial_children_mu(None, scarcity=IDLE), 0)

    def test_possible_environment_workload_has_no_cost(self):
        service = _service([[_workload()]], field="possible_environment_workload")
        with _config():
            self.assertEqual(initial_children_mu(service, scarcity=IDLE), 0)

    def test_a_group_adds_up_its_workloads(self):
        service = _service([[_workload(mem=GIB // 4), _workload(count=2, mem=2 * GIB)]])
        with _config():
            got = initial_children_mu(service, scarcity=IDLE)
            self.assertEqual(got, _balance(GIB // 4) + 2 * _balance(2 * GIB))

    def test_a_clause_costs_its_dearest_group_in_any_order(self):
        cheap, dear = [_workload(mem=GIB // 4)], [_workload(count=2, mem=2 * GIB)]
        with _config():
            one = initial_children_mu(_service([dear]), scarcity=IDLE)
            self.assertEqual(initial_children_mu(_service([cheap, dear]), scarcity=IDLE), one)
            self.assertEqual(initial_children_mu(_service([dear, cheap]), scarcity=IDLE), one)
            self.assertGreater(one, initial_children_mu(_service([cheap]), scarcity=IDLE))

    def test_the_clauses_add_up(self):
        first, second = [[_workload(mem=GIB)]], [[_workload(mem=2 * GIB)]]
        with _config():
            got = initial_children_mu(_service(first, second), scarcity=IDLE)
            self.assertEqual(got, _balance(GIB) + _balance(2 * GIB))

    def test_the_example_of_the_issue(self):
        """`(G1 or G2) and G3`: max(G1, G2) + G3."""
        g1 = [_workload(mem=3 * GIB), _workload(mem=4 * GIB)]
        g2 = [_workload(count=2, mem=GIB)]
        g3 = [_workload(count=4, mem=2 * GIB)]
        with _config():
            got = initial_children_mu(_service([g1, g2], [g3]), scarcity=IDLE)
            expected = max(_balance(3 * GIB) + _balance(4 * GIB), 2 * _balance(GIB)) \
                + 4 * _balance(2 * GIB)
        self.assertEqual(got, expected)


class TheQuoteTests(unittest.TestCase):
    """`generate_estimated_cost` and `own_price` send the figure; `cost` stays without it."""

    def _quote(self, service):
        from src.utils.cost_functions import generate_estimated_cost as module

        resources = celaut.Service.Container.Resources()
        resources.at_init.CopyFrom(_at_init())
        config = celaut.Configuration()
        config.initial_mu.n = "1000"
        with _config(), \
                patch.object(module, "compute_start_service_cost", side_effect=lambda metadata, initial_balance_mu: initial_balance_mu), \
                patch.object(module, "_service_hash_or_none", return_value=None), \
                patch.object(module, "initial_children_mu", return_value=777) as children:
            quote = module.generate_estimated_cost(
                metadata=celaut.Metadata(), config=config, resources=resources,
                service=service, check_availability=False,
            )
        return quote, children

    def test_the_cost_of_the_quote_includes_the_children(self):
        service = _service([[_workload()]])
        quote, children = self._quote(service)
        children.assert_called_once_with(service)
        self.assertEqual(from_amount(quote.cost), 1000 + 777)

    def test_own_price_has_the_children_too(self):
        from src.utils.cost_functions import delegated_price

        service = _service([[_workload()]])
        resources = celaut.Service.Container.Resources()
        resources.at_init.CopyFrom(_at_init())
        with _config(), patch.object(delegated_price, "own_build_mu", return_value=0), \
                patch.object(delegated_price, "charge_at_launch_price", return_value=0), \
                patch.object(delegated_price, "initial_children_mu", return_value=555) as children:
            price = delegated_price.own_price(
                metadata=celaut.Metadata(), configuration=celaut.Configuration(),
                resources=resources, service=service, launch_bp={"x": 1},
            )
        children.assert_called_once_with(service)
        self.assertEqual(from_amount(price.cost), 555)

    def test_own_price_takes_the_children_that_it_is_given(self):
        from src.utils.cost_functions import delegated_price

        service = _service([[_workload()]])
        configuration = celaut.Configuration()
        configuration.initial_mu.n = "1000"
        with _config(), patch.object(delegated_price, "own_build_mu", return_value=40), \
                patch.object(delegated_price, "charge_at_launch_price", return_value=0), \
                patch.object(delegated_price, "initial_children_mu") as children:
            price = delegated_price.own_price(
                metadata=celaut.Metadata(), configuration=configuration,
                resources=celaut.Service.Container.Resources(), service=service,
                launch_bp={"x": 1}, children_mu=300,
            )
        children.assert_not_called()
        self.assertEqual(from_amount(price.cost), 40 + 1000 + 300)
        # The build alone, again, with the same figure (`launch_service._delegation_terms`).
        self.assertEqual(delegated_price.peer_build_mu(price, 1000 + 300), 40)

    def test_the_default_balance_of_the_quote_is_the_service_alone(self):
        """The gateway quote does not pass the manifest to `default_initial_balance`."""
        import inspect

        from src.gateway.iterables import estimated_cost_iterable

        source = inspect.getsource(estimated_cost_iterable)
        for call in source.split("default_initial_balance(")[1:]:
            self.assertNotIn("service=", call.split(")", 1)[0])


if __name__ == "__main__":
    unittest.main()
