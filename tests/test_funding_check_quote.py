"""The funding check of a start never asks for more than the quote (#538, #636).

A client quotes a service (``GetServiceEstimatedCost``), then starts it with
``initial_mu = cost`` plus the funding sum of the periods that it wants: ``cost`` is the
build and the builds of the initial children, with no balance (#636, 9). Before #538's fix,
``workload_admission.evaluate_start_funding`` priced the children again at the load of the
start, at the worst case that the volatility bound allows, and refused a start funded
exactly as the quote said. Now the children are their builds, and the check uses the
functions of the quote at ``execution_cost.base_load``: the price with no load surcharge,
the least that any quote of this node gives while it charges.

The imports are not guarded, for the reason given in tests/test_pricing_model.py: a test of
money that is skipped says that the sums were checked when they were not.
"""

import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.utils.cost_functions import generate_estimated_cost as quote_module  # noqa: E402
from src.utils.cost_functions import start_funding  # noqa: E402
from src.utils.cost_functions import workload_admission as wa  # noqa: E402
from src.utils.cost_functions.execution_cost import CPU, DISK, MEM, base_load, is_free  # noqa: E402
from src.utils.cost_functions.start_funding import initial_children_mu  # noqa: E402
from src.utils.utils import from_amount  # noqa: E402
from tests.test_price_volatility import FULL, HALF, IDLE, _config, _guest  # noqa: E402

BUILD = 10_000_000  # pricing.BUILD_MU of the config of these tests.
BUSY_CPU = {CPU: 0.9, MEM: 0.1, DISK: 0.3}  # The load of the e2e: CPU high, the rest low.
LOADS = (IDLE, {CPU: 0.1, MEM: 0.0, DISK: 0.0}, HALF, BUSY_CPU, FULL)
LIVE = "src.utils.cost_functions.execution_cost.system_scarcity"


def _parent() -> celaut.Service:
    """A parent with one initial child of 1 GiB, 1 vCPU, as p538 of the e2e. The child
    names a service that is not built here."""
    service = celaut.Service()
    service.container.resources.at_init.CopyFrom(_guest())
    service.container.resources.at_most.CopyFrom(_guest())
    workload = service.initial_environment_workload.add().any_of.add().workloads.add(count=1)
    workload.dependency.service.container.resources.at_init.CopyFrom(_guest())
    workload.dependency.hash.add(type=b"\x01" * 32, value=bytes(range(32)))
    return service


def _unbuilt():
    return patch.object(start_funding, "is_built", return_value=False)


def _quote(service, load, initial_mu=None):
    """``cost`` of the gateway quote with the node at ``load``, and the children in it.

    The build of the parent is BUILD. The children are what ``cost`` has above the build.
    ``initial_mu`` does not change the quote (#636, 9).
    """
    resources = service.container.resources
    config = celaut.Configuration()
    if initial_mu is not None:
        config.initial_mu.n = str(initial_mu)
    with patch(LIVE, return_value=load), _unbuilt(), \
            patch.object(start_funding, "start_build_mu", return_value=BUILD), \
            patch.object(quote_module, "_service_hash_or_none", return_value=None):
        quote = quote_module.generate_estimated_cost(
            metadata=celaut.Metadata(), config=config, resources=resources,
            service=service, check_availability=False,
        )
    cost = from_amount(quote.cost)
    return cost, cost - BUILD


def _period(service, load):
    """One period of the parent, the funding sum of N = 1, at the quote's load."""
    return start_funding.minimum_runtime_mu(service.container.resources, scarcity=load)


def _check(service, initial_mu, load):
    """``evaluate_start_funding`` with the node at ``load``, and the figures it compared."""
    with patch(LIVE, return_value=load), _unbuilt(), \
            patch.object(start_funding, "start_build_mu", return_value=BUILD), \
            patch.object(start_funding, "underfunded_reason", wraps=start_funding.underfunded_reason) as reason, \
            patch.object(wa.log, "LOGGER"):
        failure = wa.evaluate_start_funding(service, celaut.Metadata(), initial_mu)
    return failure, (reason.call_args.kwargs if reason.called else None)


class TheCheckIsTheQuoteTests(unittest.TestCase):
    def test_at_the_base_load_the_check_asks_for_the_children_of_the_quote(self):
        """Same function, same inputs: the same figure, the builds of the children."""
        with _config():
            _, children = _quote(_parent(), IDLE)
            _, figures = _check(_parent(), 0, IDLE)
        self.assertEqual(children, BUILD)
        self.assertEqual(figures["children_mu"], children)

    def test_the_check_is_the_same_at_every_load_of_the_start(self):
        with _config():
            seen = {_check(_parent(), 0, load)[1]["children_mu"] for load in LOADS}
        self.assertEqual(seen, {BUILD})

    def test_a_quote_at_any_load_is_at_least_the_check(self):
        for bounded in (True, False):
            with self.subTest(bounded=bounded), _config(pricing__PRICE_BOUNDED=bounded), _unbuilt():
                floor = initial_children_mu(_parent(), scarcity=base_load())
                for load in LOADS:
                    self.assertGreaterEqual(initial_children_mu(_parent(), scarcity=load), floor)

    def test_initial_mu_does_not_change_the_quote(self):
        with _config():
            self.assertEqual(_quote(_parent(), IDLE)[0], _quote(_parent(), IDLE, 123_456)[0])


class TheQuoteFundedStartTests(unittest.TestCase):
    def test_a_start_funded_with_cost_plus_one_period_passes_at_any_load(self):
        """The quote at one load, the start at another: the drift between the two."""
        with _config():
            for quote_load in LOADS:
                cost, _ = _quote(_parent(), quote_load)
                period = _period(_parent(), quote_load)
                for start_load in LOADS:
                    with self.subTest(quote=quote_load, start=start_load):
                        failure, _ = _check(_parent(), cost + period, start_load)
                        self.assertIsNone(failure)

    def test_the_cost_alone_is_refused(self):
        """The cost has no balance: a start funded with it has no period to run."""
        with _config():
            cost, _ = _quote(_parent(), IDLE)
            failure, _ = _check(_parent(), cost, IDLE)
        self.assertIn("least initial_mu", failure)

    def test_the_e2e_case_quote_idle_start_busy_passes(self):
        """The bug of #538: quoted at a quiet moment, started with the CPU busy."""
        with _config():
            cost, children = _quote(_parent(), IDLE)
            failure, figures = _check(_parent(), cost + _period(_parent(), IDLE), BUSY_CPU)
        self.assertIsNone(failure)
        self.assertEqual(figures["children_mu"], children)

    def test_without_the_children_the_start_is_refused_at_any_load(self):
        with _config():
            cost, children = _quote(_parent(), IDLE)
            period = _period(_parent(), IDLE)
            self.assertGreater(children, 0)
            for start_load in LOADS:
                with self.subTest(start=start_load):
                    failure, _ = _check(_parent(), cost - children + period, start_load)
                    self.assertIn("initial children", failure)

    def test_one_mu_below_the_least_is_refused(self):
        with _config():
            _, figures = _check(_parent(), 0, HALF)
            least = figures["build_mu"] + figures["children_mu"] + figures["minimum_mu"]
            self.assertIsNotNone(_check(_parent(), least - 1, FULL)[0])
            self.assertIsNone(_check(_parent(), least, FULL)[0])


class TheFreeTierTests(unittest.TestCase):
    def test_a_start_while_the_node_is_free_is_not_checked(self):
        with _config(free_tier__FREE_WHILE_SCARCITY_BELOW=0.5):
            failure, figures = _check(_parent(), 0, IDLE)
        self.assertIsNone(failure)
        self.assertIsNone(figures)

    def test_a_free_node_quotes_a_cost_of_zero(self):
        # 2 (#636): the builds are free while the node is free.
        with _config(free_tier__FREE_WHILE_SCARCITY_BELOW=0.5), patch(LIVE, return_value=IDLE), \
                _unbuilt(), patch.object(start_funding, "build_charge_mu", return_value=BUILD):
            self.assertEqual(start_funding.start_cost_mu(celaut.Metadata(), _parent()), 0)

    def test_the_base_load_is_never_free(self):
        with _config(free_tier__FREE_WHILE_SCARCITY_BELOW=0.5):
            self.assertTrue(is_free(IDLE))
            self.assertFalse(is_free(base_load()))

    def test_a_busy_node_with_a_free_tier_checks_at_the_base_price(self):
        with _config(free_tier__FREE_WHILE_SCARCITY_BELOW=0.5):
            _, figures = _check(_parent(), 0, BUSY_CPU)
        with _config():
            _, children = _quote(_parent(), IDLE)
        self.assertEqual(figures["children_mu"], children)


if __name__ == "__main__":
    unittest.main()
