"""The quote has the cost of the initial children of a service (#538, absorbs #628).

``EstimatedCost.cost`` includes the builds of the children of its
``initial_environment_workload``, and no balance of them (#636, 9): each child gets its
balance in its own ``StartService``. For one workload it is ``count`` times the build of the
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


def _unbuilt():
    return patch.object(start_funding, "is_built", return_value=False)


class OneWorkloadTests(unittest.TestCase):
    def test_a_workload_with_no_service_named_costs_nothing(self):
        # No build to pay, and its balance is not in the cost (#636, 9).
        with _config():
            self.assertEqual(child_start_cost_mu(_workload(), scarcity=IDLE), 0)

    def test_count_multiplies_the_build(self):
        with _config(), _unbuilt():
            got = child_start_cost_mu(_workload(count=3, hash_value=HASH), scarcity=IDLE)
            self.assertEqual(got, 3 * BUILD)

    def test_a_service_that_is_built_here_costs_no_build(self):
        with _config(), patch.object(start_funding, "is_built", return_value=True) as built:
            got = child_start_cost_mu(_workload(hash_value=HASH), scarcity=IDLE)
            self.assertEqual(got, 0)
        built.assert_called_once_with(HASH.hex())

    def test_a_complete_service_with_no_hash_is_named_by_its_own_hash(self):
        workload = _workload()
        workload.dependency.is_completed = True
        expected = get_service_hex_main_hash(other_hashes=get_service_list_of_hashes(
            workload.dependency.service.SerializeToString()))
        with _config(), patch.object(start_funding, "is_built", return_value=False) as built:
            got = child_start_cost_mu(workload, scarcity=IDLE)
            self.assertEqual(got, BUILD)
        built.assert_called_once_with(expected)

    def test_the_resources_of_the_child_do_not_change_its_cost(self):
        with _config(), _unbuilt():
            small = child_start_cost_mu(_workload(mem=GIB // 4, hash_value=HASH), scarcity=IDLE)
            large = child_start_cost_mu(_workload(mem=8 * GIB, hash_value=HASH), scarcity=IDLE)
            none = child_start_cost_mu(_workload(resources=False, hash_value=HASH), scarcity=IDLE)
        self.assertEqual(small, BUILD)
        self.assertEqual(large, BUILD)
        self.assertEqual(none, BUILD)

    def test_count_zero_costs_nothing(self):
        with _config(), _unbuilt():
            self.assertEqual(child_start_cost_mu(_workload(count=0, hash_value=HASH), scarcity=IDLE), 0)

    def test_a_node_that_is_free_charges_no_build(self):
        # #636, 2: free, the children cost nothing in the quote. Each child still gets its
        # balance in its own StartService.
        with _config(), patch.object(start_funding, "is_free", return_value=True), _unbuilt():
            self.assertEqual(child_start_cost_mu(_workload(count=2, hash_value=HASH), scarcity=IDLE), 0)


class TheDeclarationTests(unittest.TestCase):
    def test_a_service_with_no_initial_workload_costs_nothing(self):
        with _config():
            self.assertEqual(initial_children_mu(celaut.Service(), scarcity=IDLE), 0)
            self.assertEqual(initial_children_mu(None, scarcity=IDLE), 0)

    def test_possible_environment_workload_has_no_cost(self):
        service = _service([[_workload(hash_value=HASH)]], field="possible_environment_workload")
        with _config(), _unbuilt():
            self.assertEqual(initial_children_mu(service, scarcity=IDLE), 0)

    def test_a_group_adds_up_its_workloads(self):
        service = _service([[_workload(hash_value=HASH), _workload(count=2, hash_value=HASH)]])
        with _config(), _unbuilt():
            self.assertEqual(initial_children_mu(service, scarcity=IDLE), 3 * BUILD)

    def test_a_clause_costs_its_dearest_group_in_any_order(self):
        cheap, dear = [_workload(hash_value=HASH)], [_workload(count=2, hash_value=HASH)]
        with _config(), _unbuilt():
            one = initial_children_mu(_service([dear]), scarcity=IDLE)
            self.assertEqual(initial_children_mu(_service([cheap, dear]), scarcity=IDLE), one)
            self.assertEqual(initial_children_mu(_service([dear, cheap]), scarcity=IDLE), one)
            self.assertGreater(one, initial_children_mu(_service([cheap]), scarcity=IDLE))

    def test_the_clauses_add_up(self):
        first, second = [[_workload(hash_value=HASH)]], [[_workload(count=2, hash_value=HASH)]]
        with _config(), _unbuilt():
            self.assertEqual(initial_children_mu(_service(first, second), scarcity=IDLE), 3 * BUILD)

    def test_the_example_of_the_issue(self):
        """`(G1 or G2) and G3`: max(G1, G2) + G3."""
        g1 = [_workload(hash_value=HASH), _workload(hash_value=HASH)]
        g2 = [_workload(count=3, hash_value=HASH)]
        g3 = [_workload(count=4, hash_value=HASH)]
        with _config(), _unbuilt():
            got = initial_children_mu(_service([g1, g2], [g3]), scarcity=IDLE)
        self.assertEqual(got, max(2, 3) * BUILD + 4 * BUILD)


class TheQuoteTests(unittest.TestCase):
    """`generate_estimated_cost` and `own_price`: `cost` is the builds, no balance."""

    def _quote(self, service, initial_mu="1000"):
        from src.utils.cost_functions import generate_estimated_cost as module

        resources = celaut.Service.Container.Resources()
        resources.at_init.CopyFrom(_at_init())
        config = celaut.Configuration()
        if initial_mu:
            config.initial_mu.n = initial_mu
        with _config(), \
                patch.object(start_funding, "start_build_mu", return_value=300), \
                patch.object(start_funding, "initial_children_mu", return_value=777) as children, \
                patch.object(module, "_service_hash_or_none", return_value=None):
            quote = module.generate_estimated_cost(
                metadata=celaut.Metadata(), config=config, resources=resources,
                service=service, check_availability=False,
            )
        return quote, children

    def test_the_cost_of_the_quote_is_the_builds_and_no_balance(self):
        service = _service([[_workload()]])
        quote, children = self._quote(service)
        children.assert_called_once()
        self.assertIs(children.call_args.args[0], service)
        self.assertEqual(from_amount(quote.cost), 300 + 777)

    def test_initial_mu_does_not_change_the_quote(self):
        service = _service([[_workload()]])
        with_amount, _ = self._quote(service, "1000")
        without, _ = self._quote(service, None)
        self.assertEqual(with_amount.cost, without.cost)

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
        # No balance: initial_mu is not in the cost.
        self.assertEqual(from_amount(price.cost), 40 + 300)
        # The build alone, again, with the same figure (`launch_service._delegation_terms`).
        self.assertEqual(delegated_price.own_build_of(price, 300), 40)

    def test_the_gateway_quote_does_not_fill_a_default_balance(self):
        """The gateway quote asks for no balance (#636, 9): `initial_mu` is not in `cost`."""
        import inspect

        from src.gateway.iterables import estimated_cost_iterable

        source = inspect.getsource(estimated_cost_iterable)
        self.assertNotIn("default_balance_mu", source)
        self.assertNotIn("initial_mu.CopyFrom", source)


if __name__ == "__main__":
    unittest.main()
