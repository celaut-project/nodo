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
from src.utils.cost_functions import workload_admission as wa  # noqa: E402
from src.utils.cost_functions.workload_admission import (  # noqa: E402
    ON_UNSATISFIABLE_REJECT,
    ON_UNSATISFIABLE_WARN,
    PROBE_ALL_GROUPS,
    PROBE_UNTIL_FIRST_FAILURE,
    evaluate_possible_environment_workloads,
)


def _config(**overrides):
    """Patch the module's ConfigManager reads, defaulting everything else through."""
    return patch.object(
        wa.env_manager, "get",
        side_effect=lambda key, default=None: overrides.get(key, default),
    )


def _two_unsatisfiable_groups() -> celaut.Service:
    service = celaut.Service()
    scenario = service.possible_environment_workload.add()
    scenario.workloads.add().count = 1
    scenario.workloads.add().count = 1
    scenario.workloads[0].resources.mem_limit = 111
    scenario.workloads[1].resources.mem_limit = 222
    return service


_NOTHING_FITS = patch(
    "src.utils.cost_functions.workload_admission._local_resource_availability",
    return_value={"can_execute": False, "reason": "no local memory"},
)
_NO_PEER_TAKES_IT = patch(
    "src.utils.cost_functions.workload_admission.check_resource_availability_on_peer",
    return_value=False,
)
_TWO_PEERS = patch(
    "src.utils.utils.peers_id_iterator", side_effect=lambda **_: iter(["peer-a", "peer-b"])
)


class ProbePolicyTests(unittest.TestCase):
    """How much is probed. Never *what* is admitted: every group must fit either way."""

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_fail_fast_is_the_default_and_stops_at_the_first_failure(self, peer_check, peers, local):
        with _config():
            failure = evaluate_possible_environment_workloads(_two_unsatisfiable_groups())
        self.assertIsNotNone(failure)
        self.assertIn("workloads[0]", failure)
        self.assertNotIn("workloads[1]", failure)
        # The second group was never probed, so its peers were never asked.
        self.assertEqual(local.call_count, 1)

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_full_policy_reports_every_unsatisfiable_group(self, peer_check, peers, local):
        with _config(**{"workload_admission.POLICY": PROBE_ALL_GROUPS}):
            failure = evaluate_possible_environment_workloads(_two_unsatisfiable_groups())
        self.assertIsNotNone(failure)
        self.assertIn("workloads[0]", failure)
        self.assertIn("workloads[1]", failure)
        self.assertEqual(local.call_count, 2)

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_an_unrecognised_policy_falls_back_to_fail_fast(self, peer_check, peers, local):
        with _config(**{"workload_admission.POLICY": "whatever"}):
            failure = evaluate_possible_environment_workloads(_two_unsatisfiable_groups())
        self.assertNotIn("workloads[1]", failure)

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_neither_policy_admits_a_service_whose_groups_do_not_fit(self, peer_check, peers, local):
        for policy in (PROBE_UNTIL_FIRST_FAILURE, PROBE_ALL_GROUPS):
            with self.subTest(policy=policy), _config(**{"workload_admission.POLICY": policy}):
                self.assertIsNotNone(
                    evaluate_possible_environment_workloads(_two_unsatisfiable_groups())
                )

    @patch(
        "src.utils.cost_functions.workload_admission._local_resource_availability",
        return_value={"can_execute": True, "reason": ""},
    )
    def test_fail_fast_still_probes_every_group_when_they_all_fit(self, local):
        with _config():
            self.assertIsNone(
                evaluate_possible_environment_workloads(_two_unsatisfiable_groups())
            )
        # Nothing failed, so there was nothing to stop at: both groups were checked.
        self.assertEqual(local.call_count, 2)


class OnUnsatisfiablePolicyTests(unittest.TestCase):
    """What a group that fits nowhere means for the launch."""

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_reject_is_the_default(self, peer_check, peers, local):
        with _config():
            self.assertIsNotNone(
                evaluate_possible_environment_workloads(_two_unsatisfiable_groups())
            )

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_warn_admits_the_launch_and_says_why(self, peer_check, peers, local):
        with _config(**{"workload_admission.ON_UNSATISFIABLE": ON_UNSATISFIABLE_WARN}):
            with patch("src.utils.cost_functions.workload_admission.log.LOGGER") as logger:
                self.assertIsNone(
                    evaluate_possible_environment_workloads(_two_unsatisfiable_groups())
                )
        logged = " ".join(str(call) for call in logger.call_args_list)
        self.assertIn("workloads[0]", logged)
        self.assertIn(ON_UNSATISFIABLE_WARN, logged)

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_an_unrecognised_value_falls_back_to_reject(self, peer_check, peers, local):
        with _config(**{"workload_admission.ON_UNSATISFIABLE": "maybe"}):
            self.assertIsNotNone(
                evaluate_possible_environment_workloads(_two_unsatisfiable_groups())
            )

    @patch(
        "src.utils.cost_functions.workload_admission._local_resource_availability",
        return_value={"can_execute": True, "reason": ""},
    )
    def test_warn_changes_nothing_when_everything_fits(self, local):
        with _config(**{"workload_admission.ON_UNSATISFIABLE": ON_UNSATISFIABLE_WARN}):
            self.assertIsNone(
                evaluate_possible_environment_workloads(_two_unsatisfiable_groups())
            )
        self.assertEqual(ON_UNSATISFIABLE_REJECT, "reject")


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


class RefusalMessageTests(unittest.TestCase):
    """What the refused group is described by.

    The operator reading a refusal has to be able to see which declaration caused it,
    so the message names every limit the group declares -- not a fixed three of them.
    """

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_every_declared_limit_is_named(self, peer_check, peers, local):
        service = celaut.Service()
        workload = service.possible_environment_workload.add().workloads.add()
        workload.count = 3
        workload.resources.mem_limit = 1024
        workload.resources.disk_space = 2048
        workload.resources.cpu_quota = 200000
        workload.resources.cpu_period = 50000
        workload.resources.blkio_weight = 500

        with _config():
            failure = evaluate_possible_environment_workloads(service)

        self.assertIsNotNone(failure)
        for declaration in (
                "count=3", "mem_limit=1024", "disk_space=2048",
                "cpu_quota=200000", "cpu_period=50000", "blkio_weight=500",
        ):
            self.assertIn(declaration, failure)

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_a_group_refused_over_blkio_weight_says_blkio_weight(self, peer_check, peers, local):
        # 5 is outside the 10-1000 range cgroups accept, and it is the only thing
        # wrong with this group: a message listing memory/disk/cpu would describe it
        # entirely by fields that are fine.
        service = celaut.Service()
        workload = service.possible_environment_workload.add().workloads.add()
        workload.count = 1
        workload.resources.blkio_weight = 5

        with _config():
            failure = evaluate_possible_environment_workloads(service)

        self.assertIn("blkio_weight=5", failure)

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_undeclared_limits_are_left_out_rather_than_reported_as_zero(self, peer_check, peers, local):
        service = celaut.Service()
        workload = service.possible_environment_workload.add().workloads.add()
        workload.count = 1
        workload.resources.mem_limit = 1024

        with _config():
            failure = evaluate_possible_environment_workloads(service)

        self.assertIn("mem_limit=1024", failure)
        # A limit the declaration never made is not a limit the operator has to read
        # past to find the one that matters.
        for undeclared in ("disk_space", "cpu_quota", "cpu_period", "blkio_weight"):
            self.assertNotIn(undeclared, failure)


if __name__ == "__main__":
    unittest.main()
