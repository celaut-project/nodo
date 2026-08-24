"""Unit tests for the possible_environment_workload admission check (issue #163
follow-up: the spec/packer side was done there; this is the scheduler side that
issue explicitly deferred).

``src/utils/cost_functions/workload_admission.py`` only touches bee_rpc/grpc
inside ``check_resource_availability_on_peer`` (imported lazily there), so the
module itself -- and the two functions these tests exercise -- import cleanly
without either installed. Patching ``src.utils.utils.peers_id_iterator``
still needs `src.utils.utils` importable, though, and that module does need
netifaces and two bee_rpc symbols at its own top level regardless of what we
call on it -- so, mirroring tests/test_service_registry_load.py, they are
stubbed here only when genuinely absent, and the stubs are removed again once
this file's tests are done so a later test module's own "is bee_rpc
installed" probe still sees the true environment.
"""
import sys
import types
import unittest
from unittest.mock import patch

_STUBBED_MODULES = {}


def _stub(name, **attrs):
    _STUBBED_MODULES.setdefault(name, sys.modules.get(name))
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    if "." in name:
        parent, leaf = name.rsplit(".", 1)
        if parent in sys.modules:
            setattr(sys.modules[parent], leaf, module)
    return module


try:
    import netifaces  # noqa: F401
except ImportError:
    _stub("netifaces", AF_INET=2, AF_INET6=10)

try:
    import bee_rpc  # noqa: F401
    import bee_rpc.client  # noqa: F401
    import bee_rpc.block_driver  # noqa: F401
except ImportError:
    _stub("bee_rpc")
    _stub("bee_rpc.client", Dir=type("Dir", (), {}))
    _stub("bee_rpc.block_driver", WITHOUT_BLOCK_POINTERS_FILE_NAME="without_block_pointers")


def tearDownModule():
    for name, previous in _STUBBED_MODULES.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        if "." in name:
            parent, leaf = name.rsplit(".", 1)
            if parent in sys.modules and getattr(sys.modules[parent], leaf, None) is not previous:
                if previous is None:
                    delattr(sys.modules[parent], leaf) if hasattr(sys.modules[parent], leaf) else None
                else:
                    setattr(sys.modules[parent], leaf, previous)


from protos import celaut_pb2 as celaut  # noqa: E402
from src.utils.cost_functions.workload_admission import (  # noqa: E402
    evaluate_possible_environment_workloads,
)


def _service_with_workload(count: int, mem_limit: int) -> celaut.Service:
    service = celaut.Service()
    workload = service.possible_environment_workload.add().workloads.add()
    workload.count = count
    workload.resources.mem_limit = mem_limit
    return service


class EvaluatePossibleEnvironmentWorkloadsTests(unittest.TestCase):
    def test_no_scenarios_declared_is_trivially_admitted(self):
        self.assertIsNone(evaluate_possible_environment_workloads(celaut.Service()))

    def test_workload_with_no_resources_field_is_trivially_admitted(self):
        service = celaut.Service()
        service.possible_environment_workload.add().workloads.add().count = 3
        self.assertIsNone(evaluate_possible_environment_workloads(service))

    def test_zero_count_workload_is_trivially_admitted(self):
        service = _service_with_workload(count=0, mem_limit=10**12)
        self.assertIsNone(evaluate_possible_environment_workloads(service))

    @patch(
        "src.utils.cost_functions.workload_admission._local_resource_availability",
        return_value={"can_execute": True, "reason": ""},
    )
    def test_satisfiable_locally_needs_no_peer(self, mock_availability):
        service = _service_with_workload(count=2, mem_limit=1024)
        self.assertIsNone(evaluate_possible_environment_workloads(service))
        mock_availability.assert_called_once()

    @patch(
        "src.utils.cost_functions.workload_admission.check_resource_availability_on_peer",
        return_value=True,
    )
    @patch("src.utils.utils.peers_id_iterator", side_effect=lambda **_: iter(["peer-a", "peer-b"]))
    @patch(
        "src.utils.cost_functions.workload_admission._local_resource_availability",
        return_value={"can_execute": False, "reason": "no local memory"},
    )
    def test_unsatisfiable_locally_but_a_peer_can_take_it(
            self, mock_availability, mock_peers, mock_peer_check,
    ):
        service = _service_with_workload(count=1, mem_limit=1024)
        self.assertIsNone(evaluate_possible_environment_workloads(service))
        mock_peer_check.assert_called()

    @patch(
        "src.utils.cost_functions.workload_admission.check_resource_availability_on_peer",
        return_value=None,
    )
    @patch("src.utils.utils.peers_id_iterator", side_effect=lambda **_: iter(["peer-a"]))
    @patch(
        "src.utils.cost_functions.workload_admission._local_resource_availability",
        return_value={"can_execute": False, "reason": "no local memory"},
    )
    def test_unreachable_peer_does_not_count_as_satisfiable(
            self, mock_availability, mock_peers, mock_peer_check,
    ):
        # check_resource_availability_on_peer returning None means "could not
        # ask" -- distinct from a real "no". It must not be treated as a yes.
        service = _service_with_workload(count=1, mem_limit=1024)
        failure = evaluate_possible_environment_workloads(service)
        self.assertIsNotNone(failure)
        self.assertIn("workloads[0]", failure)

    @patch(
        "src.utils.cost_functions.workload_admission._local_resource_availability",
        return_value={"can_execute": False, "reason": "no local memory"},
    )
    def test_delegate_execution_off_skips_peers_entirely(self, mock_availability):
        with patch(
            "src.utils.cost_functions.workload_admission.env_manager.get",
            side_effect=lambda key, default=None: False if key == "network.DELEGATE_EXECUTION" else default,
        ):
            with patch("src.utils.utils.peers_id_iterator") as mock_peers:
                service = _service_with_workload(count=1, mem_limit=1024)
                failure = evaluate_possible_environment_workloads(service)
                self.assertIsNotNone(failure)
                mock_peers.assert_not_called()

    @patch(
        "src.utils.cost_functions.workload_admission.check_resource_availability_on_peer",
        return_value=False,
    )
    @patch("src.utils.utils.peers_id_iterator", side_effect=lambda **_: iter(["peer-a"]))
    @patch(
        "src.utils.cost_functions.workload_admission._local_resource_availability",
        return_value={"can_execute": False, "reason": "no local memory"},
    )
    def test_reports_every_unsatisfiable_group_not_just_the_first(
            self, mock_availability, mock_peers, mock_peer_check,
    ):
        service = celaut.Service()
        scenario = service.possible_environment_workload.add()
        scenario.workloads.add().count = 1
        scenario.workloads.add().count = 1
        scenario.workloads[0].resources.mem_limit = 111
        scenario.workloads[1].resources.mem_limit = 222

        failure = evaluate_possible_environment_workloads(service)
        self.assertIsNotNone(failure)
        self.assertIn("workloads[0]", failure)
        self.assertIn("workloads[1]", failure)

    @patch(
        "src.utils.cost_functions.workload_admission._local_resource_availability",
        return_value={"can_execute": True, "reason": ""},
    )
    def test_scenarios_are_checked_independently_not_summed(self, mock_availability):
        # Two scenarios, each individually satisfiable (mocked can_execute=True
        # for any single group) -- this must not be rejected as if the demands
        # were cumulative across scenarios.
        service = celaut.Service()
        service.possible_environment_workload.add().workloads.add().count = 1
        service.possible_environment_workload.add().workloads.add().count = 1
        service.possible_environment_workload[0].workloads[0].resources.mem_limit = 10**9
        service.possible_environment_workload[1].workloads[0].resources.mem_limit = 10**9
        self.assertIsNone(evaluate_possible_environment_workloads(service))


if __name__ == "__main__":
    unittest.main()
