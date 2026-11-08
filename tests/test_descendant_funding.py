"""A service is funded for its initial children, and a start that cannot pay them is refused (#538).

A service pays the ``child_start`` of each child of its ``initial_environment_workload`` out of
its own balance. So:

* the default balance of a start with no ``initial_mu`` (``manager.default_initial_balance``)
  includes ``children_initial_cost``, and
* a start that carries its own ``initial_mu`` is refused (``workload_admission.ON_UNDERFUNDED:
  reject``) when the balance after the build cannot pay ``children_initial_cost`` and one
  maintenance period of the service. Before this, the parent started and then retried the
  start of its children in a loop (#628).

``possible_environment_workload`` is in the admission only: it adds nothing to the balance.

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
from src.utils.cost_functions import workload_admission as wa  # noqa: E402
from src.utils.cost_functions.start_funding import (  # noqa: E402
    children_initial_cost_mu,
    underfunded_reason,
)
from tests.test_price_volatility import GIB, IDLE, _config, _guest  # noqa: E402


def _workload(count=1, mem=GIB) -> celaut.Service.Workload:
    workload = celaut.Service.Workload(count=count)
    workload.dependency.service.container.resources.at_init.CopyFrom(
        celaut.Sysresources(mem_limit=mem, cpu_period=100000, cpu_quota=100000, disk_space=GIB)
    )
    return workload


def _service(*workloads, field="initial_environment_workload") -> celaut.Service:
    service = celaut.Service()
    service.container.resources.at_init.CopyFrom(_guest())
    if workloads:
        getattr(service, field).add().any_of.add().workloads.extend(workloads)
    return service


class TheDefaultBalanceTests(unittest.TestCase):
    """`default_initial_balance` with the manifest is what a start with no `initial_mu` gets."""

    def test_without_the_manifest_it_is_the_balance_of_the_service_alone(self):
        with _config():
            alone = default_initial_balance(system_resources=_guest(), scarcity=IDLE)
            self.assertGreater(alone, 0)
            self.assertEqual(
                default_initial_balance(system_resources=_guest(), scarcity=IDLE, service=celaut.Service()),
                alone,
            )

    def test_the_initial_children_are_added(self):
        service = _service(_workload(count=2))
        with _config():
            alone = default_initial_balance(system_resources=_guest(), scarcity=IDLE)
            funded = default_initial_balance(system_resources=_guest(), scarcity=IDLE, service=service)
            children = children_initial_cost_mu(service, scarcity=IDLE)
        self.assertGreater(children, 0)
        self.assertEqual(funded - alone, children)

    def test_possible_environment_workload_adds_nothing(self):
        service = _service(_workload(count=2), field="possible_environment_workload")
        with _config():
            alone = default_initial_balance(system_resources=_guest(), scarcity=IDLE)
            self.assertEqual(
                default_initial_balance(system_resources=_guest(), scarcity=IDLE, service=service), alone
            )

    def test_the_initial_children_of_a_child_are_added_to_the_child(self):
        child = _workload()
        child.dependency.service.initial_environment_workload.add().any_of.add().workloads.append(
            _workload(mem=2 * GIB))
        with _config():
            got = children_initial_cost_mu(_service(child), scarcity=IDLE)
            alone = children_initial_cost_mu(_service(_workload()), scarcity=IDLE)
            grandchild = children_initial_cost_mu(_service(_workload(mem=2 * GIB)), scarcity=IDLE)
        self.assertEqual(got, alone + grandchild)

    def test_a_service_with_no_resources_at_the_start_is_still_not_funded(self):
        with _config():
            self.assertEqual(
                default_initial_balance(system_resources=None, service=_service(_workload())), 0
            )

    def test_the_launch_and_the_local_execution_pass_the_manifest(self):
        """A default start funds the children; the gateway quote does not (it quotes them apart)."""
        import inspect

        from src.gateway.launcher import launch_service
        from src.gateway.launcher.local_execution import local_execution

        for module in (launch_service, local_execution):
            source = inspect.getsource(module)
            calls = source.split("default_initial_balance(")[1:]
            self.assertTrue(calls, module.__name__)
            for call in calls:
                self.assertIn("service=", call.split("))", 1)[0], module.__name__)


class TheShortfallTests(unittest.TestCase):
    """`underfunded_reason`: the build, the children and one period."""

    def test_enough_is_no_reason(self):
        self.assertIsNone(underfunded_reason(initial_mu=100, build_mu=10, minimum_mu=20, children_mu=70))

    def test_too_little_names_every_figure_and_the_least_amount(self):
        reason = underfunded_reason(initial_mu=99, build_mu=10, minimum_mu=20, children_mu=70)
        for text in ("initial_mu 99 MU", "leaves 89 MU", "build of 10 MU", "cost 70 MU",
                     "children_initial_cost", "costs 20 MU", "least initial_mu is 100 MU"):
            self.assertIn(text, reason)

    def test_a_service_with_no_initial_children_is_not_checked_here(self):
        self.assertIsNone(underfunded_reason(initial_mu=0, build_mu=10, minimum_mu=20, children_mu=0))


def _evaluate(service, initial_mu, **config):
    with patch.object(wa.env_manager, "get", side_effect=lambda key, default=None: config.get(key, default)), \
            patch("src.utils.cost_functions.execution_cost.system_scarcity", return_value=IDLE), \
            patch.object(start_funding, "children_initial_cost_mu", return_value=700), \
            patch.object(start_funding, "start_build_mu", return_value=100), \
            patch.object(start_funding, "minimum_runtime_mu", return_value=200), \
            patch.object(wa.log, "LOGGER") as logger:
        return wa.evaluate_start_funding(service, celaut.Metadata(), initial_mu), logger


class TheFundingCheckTests(unittest.TestCase):
    """`evaluate_start_funding` and `workload_admission.ON_UNDERFUNDED`."""

    def test_too_low_is_refused_by_default(self):
        failure, _ = _evaluate(_service(_workload()), 999)
        self.assertIn("least initial_mu is 1000 MU", failure)

    def test_enough_is_admitted(self):
        failure, _ = _evaluate(_service(_workload()), 1000)
        self.assertIsNone(failure)

    def test_warn_logs_and_admits(self):
        failure, logger = _evaluate(_service(_workload()), 1, **{"workload_admission.ON_UNDERFUNDED": "warn"})
        self.assertIsNone(failure)
        logged = " ".join(str(call) for call in logger.call_args_list)
        self.assertIn("ON_UNDERFUNDED=warn", logged)
        self.assertIn("least initial_mu is 1000 MU", logged)

    def test_an_unknown_value_is_reject(self):
        failure, _ = _evaluate(_service(_workload()), 1, **{"workload_admission.ON_UNDERFUNDED": "maybe"})
        self.assertIsNotNone(failure)

    def test_no_children_no_check(self):
        with patch("src.utils.cost_functions.execution_cost.system_scarcity", return_value=IDLE), \
                patch.object(start_funding, "start_build_mu") as build:
            self.assertIsNone(wa.evaluate_start_funding(_service(), celaut.Metadata(), 0))
        build.assert_not_called()

    def test_a_build_that_cannot_be_priced_is_counted_as_zero(self):
        with patch("src.utils.cost_functions.execution_cost.system_scarcity", return_value=IDLE), \
                patch.object(start_funding, "children_initial_cost_mu", return_value=700), \
                patch.object(start_funding, "start_build_mu", side_effect=RuntimeError("not on disk")), \
                patch.object(start_funding, "minimum_runtime_mu", return_value=200), \
                patch.object(wa.log, "LOGGER"):
            self.assertIsNone(wa.evaluate_start_funding(_service(_workload()), celaut.Metadata(), 900))
            self.assertIsNotNone(wa.evaluate_start_funding(_service(_workload()), celaut.Metadata(), 899))


class TheLaunchTests(unittest.TestCase):
    """`launch_service` applies the check after the admission and before any charge."""

    def _launch(self, configuration, funding):
        from src.gateway.launcher import launch_service as mod

        with patch.object(mod, "spend_mu") as spend, \
                patch.object(mod, "execution_balancer") as balancer, \
                patch.object(mod, "local_execution"), \
                patch.object(mod, "delegate_execution"), \
                patch.object(mod, "enforce_network_policy"), \
                patch.object(mod, "evaluate_possible_environment_workloads", return_value=None), \
                patch.object(mod, "evaluate_start_funding", return_value=funding) as check, \
                patch.object(mod, "authorize_shares"), \
                patch.object(mod, "authorize_network_shares", return_value=[]), \
                patch.object(mod, "locate_network_shares", return_value=[]), \
                patch.object(mod, "_detect_local_preflight_failure", return_value=None), \
                patch.object(mod, "descends_from_dev_client", return_value=True), \
                patch.object(mod.activity_window, "is_open", return_value=True), \
                patch.object(mod.sc, "pop_forced_execution_peer", return_value=None), \
                patch.object(mod, "get_arch_tag", return_value="linux/amd64"), \
                patch.object(mod, "default_initial_balance", return_value=4321) as default:
            balancer.return_value = iter([])
            with self.assertRaises(Exception) as ctx:
                mod.launch_service(
                    service=_service(_workload()), metadata=celaut.Metadata(),
                    configuration=configuration, father_id="vm-parent",
                    father_ip="10.0.0.1", service_id="svc-parent",
                )
        return ctx.exception, spend, balancer, check, default

    @staticmethod
    def _with_initial_mu(amount):
        configuration = celaut.Configuration()
        configuration.initial_mu.n = str(amount)
        return configuration

    def test_an_underfunded_start_is_refused_before_the_balancer_and_any_charge(self):
        error, spend, balancer, check, _ = self._launch(self._with_initial_mu(5), "the least initial_mu is 1000 MU.")
        check.assert_called_once()
        self.assertEqual(check.call_args.kwargs["initial_mu"], 5)
        self.assertIn("svc-parent", str(error))
        self.assertIn("least initial_mu is 1000 MU", str(error))
        spend.assert_not_called()
        balancer.assert_not_called()

    def test_a_funded_start_goes_on(self):
        error, _, balancer, check, _ = self._launch(self._with_initial_mu(5000), None)
        check.assert_called_once()
        balancer.assert_called_once()
        self.assertNotIn("least initial_mu", str(error))

    def test_a_start_with_no_initial_mu_is_not_checked_and_gets_the_default_with_children(self):
        configuration = celaut.Configuration()
        error, _, balancer, check, default = self._launch(configuration, "never")
        check.assert_not_called()
        balancer.assert_called_once()
        self.assertIsNotNone(default.call_args.kwargs.get("service"))
        self.assertEqual(configuration.initial_mu.n, "4321")


if __name__ == "__main__":
    unittest.main()
