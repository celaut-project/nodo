"""Unit tests for the descendant workload admission check -- the scheduler side of
the declaration issues #163 and #538 specified and the packer serializes. The
declaration is CNF: an AND of clauses, each an OR of groups, each an AND of workloads.

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


def _add_workload(group: celaut.Service.Group, count: int = 1, **limits) -> celaut.Service.Workload:
    """A workload of ``group`` whose dependency service is limited to ``limits`` (at_most)."""
    workload = group.workloads.add()
    workload.count = count
    at_most = workload.dependency.service.container.resources.at_most
    for name, value in limits.items():
        setattr(at_most, name, value)
    return workload


def _two_unsatisfiable_groups() -> celaut.Service:
    """One clause with one group of two workloads (the AND of before #538)."""
    service = celaut.Service()
    group = service.possible_environment_workload.add().any_of.add()
    _add_workload(group, mem_limit=111)
    _add_workload(group, mem_limit=222)
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
    _add_workload(service.possible_environment_workload.add().any_of.add(), count, mem_limit=mem_limit)
    return service


class EvaluatePossibleEnvironmentWorkloadsTests(unittest.TestCase):
    def test_no_scenarios_declared_is_trivially_admitted(self):
        self.assertIsNone(evaluate_possible_environment_workloads(celaut.Service()))

    def test_workload_with_no_resources_field_is_trivially_admitted(self):
        service = celaut.Service()
        group = service.possible_environment_workload.add().any_of.add()
        group.workloads.add().count = 3
        hash_only = group.workloads.add()
        hash_only.count = 1
        hash_only.dependency.hash.add(value=b"\x11" * 32)
        with patch.object(wa, "_workload_group_is_satisfiable") as probe:
            self.assertIsNone(evaluate_possible_environment_workloads(service))
        probe.assert_not_called()

    def test_a_clause_with_no_group_declares_nothing(self):
        service = celaut.Service()
        service.initial_environment_workload.add()
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
    def test_clauses_are_checked_by_existence_not_summed(self, mock_availability):
        # Two clauses, each individually satisfiable (mocked can_execute=True for any
        # single workload) -- this must not be rejected as if the demands were
        # added up across clauses.
        service = celaut.Service()
        _add_workload(service.possible_environment_workload.add().any_of.add(), mem_limit=10**9)
        _add_workload(service.possible_environment_workload.add().any_of.add(), mem_limit=10**9)
        self.assertIsNone(evaluate_possible_environment_workloads(service))


class RefusalMessageTests(unittest.TestCase):
    """What the refused group is described by.

    The operator reading a refusal has to be able to see which declaration caused it,
    so the message names every limit the group declares.
    """

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_every_declared_limit_is_named(self, peer_check, peers, local):
        service = celaut.Service()
        _add_workload(
            service.possible_environment_workload.add().any_of.add(), 3,
            mem_limit=1024, disk_space=2048, cpu_quota=200000, cpu_period=50000,
            blkio_weight=500,
        )

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
        # wrong with this group: a fixed memory/disk/cpu list describes it entirely
        # by fields that are fine.
        service = celaut.Service()
        _add_workload(service.possible_environment_workload.add().any_of.add(), blkio_weight=5)

        with _config():
            failure = evaluate_possible_environment_workloads(service)

        self.assertIn("blkio_weight=5", failure)

    @_NOTHING_FITS
    @_TWO_PEERS
    @_NO_PEER_TAKES_IT
    def test_undeclared_limits_are_left_out_rather_than_reported_as_zero(self, peer_check, peers, local):
        service = celaut.Service()
        _add_workload(service.possible_environment_workload.add().any_of.add(), mem_limit=1024)

        with _config():
            failure = evaluate_possible_environment_workloads(service)

        self.assertIn("mem_limit=1024", failure)
        # A limit the declaration never made is not a limit the operator has to read
        # past to find the one that matters.
        for undeclared in ("disk_space", "cpu_quota", "cpu_period", "blkio_weight"):
            self.assertNotIn(undeclared, failure)


GIB = 1 << 30


def _issue_example() -> celaut.Service:
    """`(G1 or G2) and G3` at the start, the example of #538.

    G1 = one instance of 3 GiB and one of 4 GiB, G2 = two instances of 1 GiB and
    4 vCPU, G3 = four instances of 2 GiB and 2 vCPU.
    """
    service = celaut.Service()
    first = service.initial_environment_workload.add()
    g1 = first.any_of.add()
    _add_workload(g1, 1, mem_limit=3 * GIB)
    _add_workload(g1, 1, mem_limit=4 * GIB)
    _add_workload(first.any_of.add(), 2, mem_limit=1 * GIB, cpu_period=100000, cpu_quota=400000)
    _add_workload(
        service.initial_environment_workload.add().any_of.add(), 4,
        mem_limit=2 * GIB, cpu_period=100000, cpu_quota=200000,
    )
    return service


def _fits_up_to(max_mem: int, asked: list):
    """A local availability that takes any shape of at most ``max_mem`` bytes."""
    def _local(request):
        asked.append(request.resources.mem_limit)
        return {"can_execute": request.resources.mem_limit <= max_mem, "reason": "memory"}
    return _local


@_TWO_PEERS
@_NO_PEER_TAKES_IT
class ConjunctiveNormalFormTests(unittest.TestCase):
    """An AND of clauses, a clause is an OR of groups, a group an AND of workloads."""

    def test_the_example_is_admitted_when_only_one_alternative_fits(self, peer_check, peers):
        asked = []
        with _config(), patch.object(wa, "_local_resource_availability", side_effect=_fits_up_to(2 * GIB, asked)):
            self.assertIsNone(evaluate_possible_environment_workloads(_issue_example()))
        # G1 stops at its 3 GiB workload, G2 (1 GiB) fits, G3 (2 GiB) fits.
        self.assertEqual(asked, [3 * GIB, 1 * GIB, 2 * GIB])

    def test_the_check_of_a_clause_stops_at_the_first_group_that_fits(self, peer_check, peers):
        asked = []
        with _config(), patch.object(wa, "_local_resource_availability", side_effect=_fits_up_to(8 * GIB, asked)):
            self.assertIsNone(evaluate_possible_environment_workloads(_issue_example()))
        # G1 fits whole, so G2 is never asked about.
        self.assertEqual(asked, [3 * GIB, 4 * GIB, 2 * GIB])

    def test_a_clause_where_no_group_fits_refuses_and_names_every_group(self, peer_check, peers):
        asked = []
        with _config(), patch.object(wa, "_local_resource_availability", side_effect=_fits_up_to(GIB // 2, asked)):
            failure = evaluate_possible_environment_workloads(_issue_example())
        self.assertIn("initial_environment_workload[0]: no group fits", failure)
        self.assertIn("initial_environment_workload[0].any_of[0].workloads[0]", failure)
        self.assertIn("initial_environment_workload[0].any_of[1].workloads[0]", failure)
        # fail_fast: the second clause is not probed once the first one fails.
        self.assertNotIn("initial_environment_workload[1]", failure)

    def test_full_policy_names_every_clause_and_workload_that_fails(self, peer_check, peers):
        asked = []
        with _config(**{"workload_admission.POLICY": PROBE_ALL_GROUPS}), \
                patch.object(wa, "_local_resource_availability", side_effect=_fits_up_to(GIB // 2, asked)):
            failure = evaluate_possible_environment_workloads(_issue_example())
        for path in (
                "initial_environment_workload[0].any_of[0].workloads[0]",
                "initial_environment_workload[0].any_of[0].workloads[1]",
                "initial_environment_workload[0].any_of[1].workloads[0]",
                "initial_environment_workload[1].any_of[0].workloads[0]",
        ):
            self.assertIn(path, failure)

    def test_the_last_clause_must_fit_too(self, peer_check, peers):
        asked = []
        with _config(), patch.object(wa, "_local_resource_availability", side_effect=_fits_up_to(GIB, asked)):
            failure = evaluate_possible_environment_workloads(_issue_example())
        # G2 fits the first clause, G3 (2 GiB) does not fit the second.
        self.assertIn("initial_environment_workload[1]: no group fits", failure)
        self.assertNotIn("initial_environment_workload[0]", failure)

    def test_both_fields_are_checked(self, peer_check, peers):
        service = celaut.Service()
        _add_workload(service.initial_environment_workload.add().any_of.add(), mem_limit=GIB)
        _add_workload(service.possible_environment_workload.add().any_of.add(), mem_limit=4 * GIB)
        asked = []
        with _config(), patch.object(wa, "_local_resource_availability", side_effect=_fits_up_to(2 * GIB, asked)):
            failure = evaluate_possible_environment_workloads(service)
        self.assertIn("possible_environment_workload[0]: no group fits", failure)
        self.assertEqual(asked, [GIB, 4 * GIB])


class GroupRequestTests(unittest.TestCase):
    """The shape of a workload is the one of its dependency service (#538)."""

    def test_limits_of_at_most_and_benchmark_of_at_init(self):
        workload = celaut.Service.Workload(count=1)
        container = workload.dependency.service.container
        container.architecture.tags.append("linux/arm64")
        container.resources.at_init.mem_limit = 1
        container.resources.at_init.benchmark.add(key="int_ops_per_sec", value=500)
        container.resources.at_most.mem_limit = 7
        container.resources.at_most.disk_space = 9
        container.resources.at_most.benchmark.add(key="flt_ops_per_sec", value=1)

        request = wa.group_request(workload)

        self.assertEqual(list(request.architecture.tags), ["linux/arm64"])
        self.assertEqual(request.resources.mem_limit, 7)
        self.assertEqual(request.resources.disk_space, 9)
        # at_most.benchmark has no meaning; the minimum is at_init's.
        self.assertEqual([(e.key, e.value) for e in request.resources.benchmark], [("int_ops_per_sec", 500)])

    def test_a_service_with_no_at_most_is_limited_by_at_init(self):
        workload = celaut.Service.Workload(count=1)
        workload.dependency.service.container.resources.at_init.mem_limit = 5
        self.assertEqual(wa.group_request(workload).resources.mem_limit, 5)

    def test_a_workload_with_no_service_asks_for_nothing(self):
        workload = celaut.Service.Workload(count=1)
        workload.dependency.hash.add(value=b"\x22" * 32)
        self.assertFalse(wa.group_request(workload).ListFields())


if __name__ == "__main__":
    unittest.main()
