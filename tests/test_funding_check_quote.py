"""The funding check of a start never asks for more than the quote (#538).

A client quotes a service (``GetServiceEstimatedCost``), then starts it with
``initial_mu = cost + children_initial_cost`` (plus the maintenance it wants). Before this
fix, ``workload_admission.evaluate_start_funding`` priced the children again at the load of
the start, at the worst case that the volatility bound allows. With the load up at the
start, it asked for up to ``SCARCITY_MAX_MULTIPLIER`` times the base price (41.75M MU
against quotes of 9.7M to 27.2M on a test node) and refused a start funded exactly as the
quote said.

Now the check uses the functions of the quote at ``execution_cost.base_load``: the price
with no load surcharge, the least that any quote of this node gives while it charges.

The imports are not guarded, for the reason given in tests/test_pricing_model.py: a test of
money that is skipped says that the sums were checked when they were not.
"""

import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.manager.manager import default_initial_balance  # noqa: E402
from src.utils.cost_functions import generate_estimated_cost as quote_module  # noqa: E402
from src.utils.cost_functions import start_funding  # noqa: E402
from src.utils.cost_functions import workload_admission as wa  # noqa: E402
from src.utils.cost_functions.execution_cost import CPU, DISK, MEM, base_load, is_free  # noqa: E402
from src.utils.cost_functions.start_funding import children_initial_cost_mu  # noqa: E402
from src.utils.utils import from_amount  # noqa: E402
from tests.test_price_volatility import FULL, HALF, IDLE, _config, _guest  # noqa: E402

BUILD = 10_000_000  # pricing.BUILD_MU of the config of these tests.
BUSY_CPU = {CPU: 0.9, MEM: 0.1, DISK: 0.3}  # The load of the e2e: CPU high, the rest low.
LOADS = (IDLE, {CPU: 0.1, MEM: 0.0, DISK: 0.0}, HALF, BUSY_CPU, FULL)
LIVE = "src.utils.cost_functions.execution_cost.system_scarcity"


def _parent() -> celaut.Service:
    """A parent with one initial child of 1 GiB, 1 vCPU, as p538 of the e2e."""
    service = celaut.Service()
    service.container.resources.at_init.CopyFrom(_guest())
    service.container.resources.at_most.CopyFrom(_guest())
    workload = service.initial_environment_workload.add().any_of.add().workloads.add(count=1)
    workload.dependency.service.container.resources.at_init.CopyFrom(_guest())
    return service


def _quote(service, load):
    """``cost`` and ``children_initial_cost`` of the gateway quote, with the node at ``load``.

    The default balance of the service alone, as ``EstimatedCostIterable._quote`` gives it;
    the build of the parent is BUILD.
    """
    resources = service.container.resources
    config = celaut.Configuration()
    with patch(LIVE, return_value=load), \
            patch.object(quote_module, "compute_start_service_cost",
                         side_effect=lambda metadata, initial_balance_mu: BUILD + initial_balance_mu), \
            patch.object(quote_module, "_service_hash_or_none", return_value=None):
        config.initial_mu.n = str(default_initial_balance(system_resources=resources.at_init))
        quote = quote_module.generate_estimated_cost(
            metadata=celaut.Metadata(), config=config, resources=resources,
            service=service, check_availability=False,
        )
    return from_amount(quote.cost), from_amount(quote.children_initial_cost)


def _check(service, initial_mu, load):
    """``evaluate_start_funding`` with the node at ``load``, and the figures it compared."""
    with patch(LIVE, return_value=load), \
            patch.object(start_funding, "start_build_mu", return_value=BUILD), \
            patch.object(start_funding, "underfunded_reason", wraps=start_funding.underfunded_reason) as reason, \
            patch.object(wa.log, "LOGGER"):
        failure = wa.evaluate_start_funding(service, celaut.Metadata(), initial_mu)
    return failure, (reason.call_args.kwargs if reason.called else None)


class TheCheckIsTheQuoteTests(unittest.TestCase):
    def test_at_the_base_load_the_check_asks_for_the_children_of_the_quote(self):
        """Same function, same inputs: the same figure."""
        with _config():
            _, children = _quote(_parent(), IDLE)
            _, figures = _check(_parent(), 0, IDLE)
        self.assertGreater(children, 0)
        self.assertEqual(figures["children_mu"], children)

    def test_the_check_is_the_same_at_every_load_of_the_start(self):
        with _config():
            seen = {_check(_parent(), 0, load)[1]["children_mu"] for load in LOADS}
        self.assertEqual(len(seen), 1)

    def test_a_quote_at_any_load_is_at_least_the_check(self):
        for bound in (2.0, 0):
            with self.subTest(bound=bound), _config(pricing__MAX_PRICE_VOLATILITY=bound):
                floor = children_initial_cost_mu(_parent(), scarcity=base_load())
                for load in LOADS:
                    self.assertGreaterEqual(children_initial_cost_mu(_parent(), scarcity=load), floor)


class TheQuoteFundedStartTests(unittest.TestCase):
    def test_a_start_funded_as_the_quote_says_passes_at_any_load(self):
        """The quote at one load, the start at another: the drift between the two."""
        with _config():
            for quote_load in LOADS:
                cost, children = _quote(_parent(), quote_load)
                for start_load in LOADS:
                    with self.subTest(quote=quote_load, start=start_load):
                        failure, _ = _check(_parent(), cost + children, start_load)
                        self.assertIsNone(failure)

    def test_the_e2e_case_quote_idle_start_busy_passes(self):
        """The bug: quoted at a quiet moment, started with the CPU busy."""
        with _config():
            cost, children = _quote(_parent(), IDLE)
            failure, figures = _check(_parent(), cost + children, BUSY_CPU)
            busy_children = children_initial_cost_mu(_parent(), scarcity=BUSY_CPU)
        self.assertIsNone(failure)
        self.assertEqual(figures["children_mu"], children)
        self.assertGreater(busy_children, 2 * children)  # What the old check asked for.

    def test_without_the_children_the_start_is_refused_at_any_load(self):
        with _config():
            cost, children = _quote(_parent(), IDLE)
            for start_load in LOADS:
                with self.subTest(start=start_load):
                    failure, _ = _check(_parent(), cost, start_load)
                    self.assertIn("children_initial_cost", failure)

    def test_one_mu_below_the_least_is_refused(self):
        with _config():
            _, figures = _check(_parent(), 0, HALF)
            least = figures["build_mu"] + figures["children_mu"] + figures["minimum_mu"]
            self.assertIsNotNone(_check(_parent(), least - 1, FULL)[0])
            self.assertIsNone(_check(_parent(), least, FULL)[0])

    def test_below_the_quote_by_the_children_is_refused(self):
        with _config():
            _, children = _quote(_parent(), IDLE)
            failure, _ = _check(_parent(), BUILD + children - 1, IDLE)
        self.assertIsNotNone(failure)


class TheFreeTierTests(unittest.TestCase):
    def test_a_start_while_the_node_is_free_is_not_checked(self):
        with _config(free_tier__FREE_WHILE_SCARCITY_BELOW=0.5):
            failure, figures = _check(_parent(), 0, IDLE)
        self.assertIsNone(failure)
        self.assertIsNone(figures)

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
