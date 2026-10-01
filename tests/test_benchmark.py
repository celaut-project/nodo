"""`Sysresources.benchmark` (#448, #459): the per-core minimum benchmark a service requires.

Four things are pinned here:

* **the wire** -- the map round-trips, an unrecognised key included, and a node built
  before the field existed still reads every other field of a message that carries it;
* **service.json** -- what the packer accepts, what it refuses, and that a service that
  never mentions the field serializes to exactly what it did before;
* **admission** -- `at_init.benchmark` is enforced per architecture against this node's
  measured scores from config.yaml, an unmeasured (-1) one is only logged, and
  `at_most.benchmark` means nothing;
* **delegation** -- the requirement reaches the peer that is asked to run the service.
"""
import unittest
from unittest.mock import patch

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory

from tests.config_bootstrap import load_example_config

# Before anything that builds a ConfigManager at import: the shipped example points
# STORAGE at /nodo, which only exists on an installed node.
load_example_config()

from protos import celaut_pb2 as celaut, pack_pb2  # noqa: E402
from src.packers.service_json import (  # noqa: E402
    parse_service_spec,
    populate_possible_environment_workloads,
)
from src.utils import keyvalue, benchmark  # noqa: E402
from src.utils.cost_functions import resource_availability as ra  # noqa: E402
from src.utils.cost_functions.architecture_resources import ask_of as ar_ask  # noqa: E402
from src.utils.cost_functions import workload_admission as wa  # noqa: E402
from src.utils.benchmark import BENCHMARK_KEYS, parse_benchmark  # noqa: E402

PACKER_IMPORT_ERROR = None
try:
    from src.packers.zip_with_dockerfile import ZipContainerPacker
except Exception as import_exc:  # pragma: no cover - environment-dependent
    PACKER_IMPORT_ERROR = import_exc
    ZipContainerPacker = None  # type: ignore[assignment]


def _pre_448_sysresources():
    """`Sysresources` as a node built before #448 compiled it: fields 1-5, no field 6.

    Built in a pool of its own, so it shares nothing with the generated module but the
    field numbers -- which is all two nodes on different releases share either.
    """
    file = descriptor_pb2.FileDescriptorProto(
        name="pre_448_sysresources.proto", package="pre448", syntax="proto3"
    )
    message = file.message_type.add(name="Sysresources")
    for number, name in enumerate(
            ("blkio_weight", "cpu_period", "cpu_quota", "mem_limit", "disk_space"), start=1
    ):
        message.field.add(
            name=name,
            number=number,
            type=descriptor_pb2.FieldDescriptorProto.TYPE_UINT64,
            label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
            proto3_optional=True,
            oneof_index=number - 1,
        )
        message.oneof_decl.add(name=f"_{name}")
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("pre448.Sysresources"))


class WireTests(unittest.TestCase):

    def test_the_vocabulary_is_the_named_primitives_one_bandwidth_per_working_set(self):
        self.assertEqual(
            BENCHMARK_KEYS,
            (
                "int_ops_per_sec",
                "flt_ops_per_sec",
                "mem_bandwidth_64mib_bytes_per_sec",
                "mem_bandwidth_256mib_bytes_per_sec",
                "mem_bandwidth_1gib_bytes_per_sec",
                "sha256_hashes_per_sec",
            ),
        )

    def test_it_is_field_99_a_repeated_string_to_uint64_entry(self):
        # key=1/value=2 keep the entry wire-identical to the `map<string, uint64>` it
        # replaced (see tests/test_keyvalue_wire.py); the field itself moved from 6 to 99.
        field = celaut.Sysresources.DESCRIPTOR.fields_by_name["benchmark"]
        self.assertEqual(field.number, 99)
        self.assertEqual(field.label, field.LABEL_REPEATED)
        entry = field.message_type
        self.assertEqual(entry.full_name, "celaut.Uint64KeyValue")
        self.assertFalse(entry.GetOptions().map_entry)
        self.assertEqual(entry.fields_by_name["key"].number, 1)
        self.assertEqual(entry.fields_by_name["value"].number, 2)
        self.assertEqual(entry.fields_by_name["key"].type, entry.fields_by_name["key"].TYPE_STRING)
        self.assertEqual(entry.fields_by_name["value"].type, entry.fields_by_name["value"].TYPE_UINT64)

    def test_round_trip_keeps_every_key_recognised_or_not(self):
        sent = celaut.Sysresources(cpu_quota=200000, cpu_period=100000)
        keyvalue.set_value(sent.benchmark, "int_ops_per_sec", 500000)
        keyvalue.set_value(sent.benchmark, "sha256_hashes_per_sec", 2 ** 64 - 1)
        keyvalue.set_value(sent.benchmark, "a_primitive_from_the_future", 7)

        received = celaut.Sysresources()
        received.ParseFromString(sent.SerializeToString())

        self.assertEqual(
            keyvalue.to_dict(received.benchmark),
            {
                "int_ops_per_sec": 500000,
                "sha256_hashes_per_sec": 2 ** 64 - 1,
                "a_primitive_from_the_future": 7,
            },
        )
        self.assertEqual(received, sent)
        self.assertEqual(
            benchmark.unrecognised_keys(keyvalue.to_dict(received.benchmark)),
            ("a_primitive_from_the_future",),
        )

    def test_an_absent_key_is_absent_not_zero(self):
        sysreq = celaut.Sysresources()
        keyvalue.set_value(sysreq.benchmark, "int_ops_per_sec", 1)
        self.assertFalse(keyvalue.contains(sysreq.benchmark, "flt_ops_per_sec"))
        self.assertIsNone(keyvalue.get(sysreq.benchmark, "flt_ops_per_sec"))

    def test_a_message_without_it_serializes_as_before(self):
        # The service spec is hashed into the service id, so the field existing must
        # not move a single byte of a service that does not use it.
        sysreq = celaut.Sysresources(
            blkio_weight=500, cpu_period=100000, cpu_quota=50000, mem_limit=1024, disk_space=2048
        )
        old = _pre_448_sysresources()(
            blkio_weight=500, cpu_period=100000, cpu_quota=50000, mem_limit=1024, disk_space=2048
        )
        self.assertEqual(sysreq.SerializeToString(), old.SerializeToString())

    def test_a_pre_448_reader_skips_the_field_and_keeps_the_rest(self):
        sent = celaut.Sysresources(
            blkio_weight=500, cpu_period=100000, cpu_quota=200000, mem_limit=1024, disk_space=2048
        )
        keyvalue.set_value(sent.benchmark, "int_ops_per_sec", 500000)
        keyvalue.set_value(sent.benchmark, "unknown_primitive", 9)

        old = _pre_448_sysresources()()
        old.ParseFromString(sent.SerializeToString())

        self.assertEqual(
            (old.blkio_weight, old.cpu_period, old.cpu_quota, old.mem_limit, old.disk_space),
            (500, 100000, 200000, 1024, 2048),
        )
        self.assertFalse(hasattr(old, "benchmark"))

    def test_a_pre_448_relay_does_not_strip_it(self):
        # An old node that parses and re-serializes keeps field 6 as unknown bytes, so
        # a requirement survives being forwarded through a peer that cannot read it.
        sent = celaut.Sysresources(mem_limit=1024)
        keyvalue.set_value(sent.benchmark, "flt_ops_per_sec", 42)

        old = _pre_448_sysresources()()
        old.ParseFromString(sent.SerializeToString())
        received = celaut.Sysresources()
        received.ParseFromString(old.SerializeToString())

        self.assertEqual(keyvalue.to_dict(received.benchmark), {"flt_ops_per_sec": 42})
        self.assertEqual(received.mem_limit, 1024)

    def test_a_pre_448_message_reads_as_no_requirement(self):
        old = _pre_448_sysresources()(mem_limit=1024, cpu_quota=100000)
        received = celaut.Sysresources()
        received.ParseFromString(old.SerializeToString())
        self.assertEqual(keyvalue.to_dict(received.benchmark), {})
        self.assertEqual(received.mem_limit, 1024)

    def test_it_survives_the_pack_schema_to_celaut_schema_reread(self):
        # ZipContainerPacker.save re-reads the pack.Service bytes as a celaut.Service.
        packed = pack_pb2.Service()
        keyvalue.set_value(packed.container.resources.at_most.benchmark, "int_ops_per_sec", 5)
        spec = celaut.Service()
        spec.ParseFromString(packed.SerializeToString())
        self.assertEqual(keyvalue.to_dict(spec.container.resources.at_most.benchmark), {"int_ops_per_sec": 5})


class ParseBenchmarkTests(unittest.TestCase):

    def test_absent_is_no_requirement(self):
        self.assertEqual(parse_benchmark(None, "resources.at_most.benchmark"), {})
        self.assertEqual(parse_benchmark({}, "resources.at_most.benchmark"), {})

    def test_valid_values_are_kept_sorted_by_key(self):
        parsed = parse_benchmark(
            {"sha256_hashes_per_sec": 1000000, "int_ops_per_sec": 500000, "flt_ops_per_sec": 0},
            "resources.at_most.benchmark",
        )
        self.assertEqual(
            list(parsed.items()),
            [("flt_ops_per_sec", 0), ("int_ops_per_sec", 500000), ("sha256_hashes_per_sec", 1000000)],
        )

    def test_an_unrecognised_key_is_kept_not_refused(self):
        self.assertEqual(
            parse_benchmark({"gpu_matmul_per_sec": 3}, "resources.at_most.benchmark"),
            {"gpu_matmul_per_sec": 3},
        )

    def test_the_largest_uint64_is_accepted(self):
        self.assertEqual(
            parse_benchmark({"int_ops_per_sec": 2 ** 64 - 1}, "p"), {"int_ops_per_sec": 2 ** 64 - 1}
        )

    def test_anything_but_a_non_negative_integer_is_refused_naming_the_key(self):
        for bad in (-1, 1.5, 500000.0, "500000", True, False, None, [1], {"n": 1}, 2 ** 64):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as raised:
                    parse_benchmark(
                        {"int_ops_per_sec": bad}, "resources.at_most.benchmark"
                    )
                self.assertIn(
                    "resources.at_most.benchmark.int_ops_per_sec", str(raised.exception)
                )

    def test_anything_but_an_object_is_refused(self):
        for bad in (500000, "int_ops_per_sec", ["int_ops_per_sec"], True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as raised:
                    parse_benchmark(bad, "resources.at_init.benchmark")
                self.assertIn("resources.at_init.benchmark must be an object", str(raised.exception))

    def test_an_empty_key_is_refused(self):
        with self.assertRaises(ValueError):
            parse_benchmark({"": 1}, "resources.at_most.benchmark")


def _packer(service_json):
    """A ZipContainerPacker carrying only the json `_benchmarks` reads.

    __init__ drives BuildKit; this method reads `self.json` and nothing else.
    """
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = service_json
    return packer


@unittest.skipIf(
    PACKER_IMPORT_ERROR is not None, f"Missing runtime dependencies: {PACKER_IMPORT_ERROR}"
)
class PackerServiceJsonTests(unittest.TestCase):

    def test_absent_is_no_requirement(self):
        for service_json in ({}, {"resources": {}}, {"resources": {"at_init": {}, "at_most": {}}}):
            with self.subTest(service_json=service_json):
                self.assertEqual(_packer(service_json)._benchmarks(), {})

    def test_it_is_read_from_at_init(self):
        self.assertEqual(
            _packer({"resources": {"at_init": {"benchmark": {"int_ops_per_sec": 100}}}})._benchmarks(),
            {"int_ops_per_sec": 100},
        )

    def test_under_at_most_it_has_no_meaning_and_is_refused(self):
        # A floor is what at_init says; packed under at_most it would change the
        # service's hash and be ignored by every node (#459).
        with self.assertRaises(ValueError) as raised:
            _packer({"resources": {"at_most": {"benchmark": {"int_ops_per_sec": 300}}}})._benchmarks()
        self.assertIn("resources.at_init.benchmark", str(raised.exception))

    def test_the_old_min_benchmark_key_is_refused_naming_the_new_one(self):
        for end in ("at_init", "at_most"):
            with self.subTest(end=end):
                with self.assertRaises(ValueError) as raised:
                    _packer({"resources": {end: {"min_benchmark": {"int_ops_per_sec": 1}}}})._benchmarks()
                self.assertIn("resources.at_init.benchmark", str(raised.exception))

    def test_a_malformed_value_is_refused_naming_it(self):
        for bad in (-5, 2.5, "9"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as raised:
                    _packer({"resources": {"at_init": {"benchmark": {"flt_ops_per_sec": bad}}}})._benchmarks()
                self.assertIn("resources.at_init.benchmark.flt_ops_per_sec", str(raised.exception))

    def test_it_is_refused_when_service_json_is_read_not_after_the_build(self):
        packer = _packer({"resources": {"at_init": {"benchmark": {"int_ops_per_sec": -1}}}})
        with self.assertRaises(ValueError):
            packer._validate_service_json_shape()


class NestedServiceJsonTests(unittest.TestCase):
    """Workload groups and embedded dependency services are protobuf-JSON `Sysresources`."""

    def _workloads(self, resources):
        service = pack_pb2.Service()
        populate_possible_environment_workloads(
            service, [{"workloads": [{"count": 1, "resources": resources}]}]
        )
        return service.possible_environment_workload[0].workloads[0].resources

    def test_a_workload_group_carries_it(self):
        resources = self._workloads(
            {"mem_limit": 100, "benchmark": {"int_ops_per_sec": 500000, "unknown_primitive": 1}}
        )
        self.assertEqual(
            keyvalue.to_dict(resources.benchmark), {"int_ops_per_sec": 500000, "unknown_primitive": 1}
        )
        self.assertEqual(resources.mem_limit, 100)

    def test_a_workload_group_without_it_has_none(self):
        self.assertEqual(dict(self._workloads({"mem_limit": 100}).benchmark), {})

    def test_a_workload_group_refuses_negative_and_non_integer_values(self):
        for bad in (-1, 1.5, True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self._workloads({"benchmark": {"int_ops_per_sec": bad}})

    def test_an_embedded_dependency_service_carries_it(self):
        service = parse_service_spec({"container": {"resources": {
            "at_most": {"benchmark": {"mem_bandwidth_1gib_bytes_per_sec": 1000}},
        }}})
        self.assertEqual(
            keyvalue.to_dict(service.container.resources.at_most.benchmark),
            {"mem_bandwidth_1gib_bytes_per_sec": 1000},
        )

    def test_protobuf_json_round_trip(self):
        sysreq = celaut.Sysresources()
        keyvalue.set_value(sysreq.benchmark, "int_ops_per_sec", 5)
        again = json_format.ParseDict(json_format.MessageToDict(sysreq), celaut.Sysresources())
        self.assertEqual(again, sysreq)


def _resources(**benchmarks) -> celaut.Service.Container.Resources:
    resources = celaut.Service.Container.Resources(
        at_most=celaut.Sysresources(mem_limit=1024, cpu_quota=100000, cpu_period=100000)
    )
    keyvalue.update(resources.at_init.benchmark, benchmarks)
    return resources


def _scores(**measured):
    scores = {key: benchmark.UNMEASURED for key in benchmark.SCORE_KEYS}
    scores.update(measured)
    return scores


class AdmissionTests(unittest.TestCase):
    """Enforced per architecture against this node's measured scores; -1 stays log-only."""

    def _availability(self, resources, arch="linux/amd64", **scores):
        asked = []

        def _node_scores(a):
            asked.append(a)
            return _scores(**scores)

        with patch.object(ra, "could_ve_this_sysreq", return_value=True), \
                patch.object(ra.host_limits, "ceiling_shortfalls", return_value=[]), \
                patch.object(ra.benchmark, "node_scores", side_effect=_node_scores), \
                patch.object(ra.log, "LOGGER") as logger:
            availability = ra.get_resource_availability(resources, arch=arch)
        self.asked_arch = asked
        return availability, [c.args[0] for c in logger.call_args_list]

    def test_a_measured_score_below_the_requirement_is_a_shortfall(self):
        availability, _ = self._availability(
            _resources(int_ops_per_sec=500000), int_ops_per_sec=20000
        )
        self.assertFalse(availability["can_execute"])
        self.assertIn("resources.at_init.benchmark.int_ops_per_sec", availability["reason"])
        self.assertIn("on this node for linux/amd64: 20000", availability["reason"])

    def test_a_score_that_meets_it_admits(self):
        availability, lines = self._availability(
            _resources(int_ops_per_sec=500000), int_ops_per_sec=900000
        )
        self.assertTrue(availability["can_execute"])
        self.assertEqual(lines, [])

    def test_the_scores_read_are_the_requested_architectures(self):
        self._availability(_resources(int_ops_per_sec=1), arch="linux/arm64")
        self.assertEqual(self.asked_arch, ["linux/arm64"])

    def test_no_architecture_is_the_hosts_own(self):
        with patch.object(ra, "host_arch_tag", return_value="linux/arm64"):
            self._availability(_resources(int_ops_per_sec=1), arch=None)
        self.assertEqual(self.asked_arch, ["linux/arm64"])

    def test_an_unmeasured_score_is_logged_not_enforced(self):
        availability, lines = self._availability(
            _resources(sha256_hashes_per_sec=9, int_ops_per_sec=2 ** 64 - 1)
        )
        self.assertTrue(availability["can_execute"])
        self.assertEqual(availability["reason"], "")
        self.assertEqual(len(lines), 1)
        self.assertIn("int_ops_per_sec=18446744073709551615, sha256_hashes_per_sec=9", lines[0])
        self.assertIn("not enforced", lines[0])

    def test_enforced_and_unenforced_primitives_split_cleanly(self):
        availability, lines = self._availability(
            _resources(int_ops_per_sec=10, flt_ops_per_sec=10), int_ops_per_sec=1
        )
        self.assertFalse(availability["can_execute"])
        self.assertIn("int_ops_per_sec", availability["reason"])
        self.assertNotIn("flt_ops_per_sec", availability["reason"])
        self.assertIn("flt_ops_per_sec=10", lines[0])

    def test_an_unrecognised_primitive_is_named_and_is_not_an_error(self):
        availability, lines = self._availability(_resources(quantum_ops_per_sec=3))
        self.assertTrue(availability["can_execute"])
        self.assertIn("Unrecognised primitive(s): quantum_ops_per_sec.", lines[0])

    def test_at_most_benchmark_carries_no_meaning_and_is_ignored(self):
        resources = _resources()
        keyvalue.set_value(resources.at_most.benchmark, "int_ops_per_sec", 2 ** 64 - 1)
        availability, lines = self._availability(resources, int_ops_per_sec=1)
        self.assertTrue(availability["can_execute"])
        self.assertIn("ignored", lines[0])

    def test_bandwidth_is_held_against_the_score_over_the_same_or_next_larger_working_set(self):
        key_256, key_1gib = "mem_bandwidth_256mib_bytes_per_sec", "mem_bandwidth_1gib_bytes_per_sec"
        resources = _resources(**{key_256: 10 ** 9})
        ok, _ = self._availability(resources, **{key_256: 10 ** 10})
        slow, _ = self._availability(resources, **{key_256: 10 ** 8})
        # With no score over its own working set it is read against the next larger one.
        larger_ok, _ = self._availability(resources, **{key_1gib: 10 ** 10})
        larger_slow, _ = self._availability(resources, **{key_1gib: 10 ** 8})
        # A requirement over a working set larger than any measured is only logged.
        beyond, lines = self._availability(
            _resources(mem_bandwidth_4gib_bytes_per_sec=10 ** 12), **{key_1gib: 1}
        )
        self.assertTrue(ok["can_execute"])
        self.assertFalse(slow["can_execute"])
        self.assertIn(key_256, slow["reason"])
        self.assertTrue(larger_ok["can_execute"])
        self.assertFalse(larger_slow["can_execute"])
        self.assertIn(f"(over {key_1gib})", larger_slow["reason"])
        self.assertTrue(beyond["can_execute"])
        self.assertTrue(any("is not enforced" in line for line in lines))

    def test_no_requirement_no_log_line(self):
        _, lines = self._availability(_resources())
        self.assertEqual(lines, [])

    def test_shortfalls_are_joined_with_the_other_limits(self):
        resources = _resources(int_ops_per_sec=10)
        with patch.object(ra, "could_ve_this_sysreq", return_value=False), \
                patch.object(ra.host_limits, "ceiling_shortfalls", return_value=[]), \
                patch.object(ra.benchmark, "node_scores", return_value=_scores(int_ops_per_sec=1)), \
                patch.object(ra.log, "LOGGER"):
            reason = ra.get_resource_availability(resources, arch="linux/amd64")["reason"]
        self.assertIn("Insufficient memory", reason)
        self.assertIn(" | ", reason)
        self.assertIn("int_ops_per_sec", reason)

    def test_admission_never_imports_a_virtualizer(self):
        # It reads config.yaml; measuring is the core service's job. A fresh interpreter,
        # so nothing this test process imported earlier can hide an import.
        import subprocess
        import sys

        code = (
            "from tests.config_bootstrap import load_example_config; load_example_config()\n"
            "import sys\n"
            "from protos import celaut_pb2 as c\n"
            "from src.utils.cost_functions import resource_availability as ra\n"
            "r = c.Service.Container.Resources()\n"
            "r.at_init.benchmark.add(key='int_ops_per_sec', value=1)\n"
            "ra.get_resource_availability(r, arch='linux/amd64')\n"
            "print(sorted(m for m in sys.modules if m.startswith('src.virtualizers')))\n"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "[]")


class ArchitectureAvailabilityTests(unittest.TestCase):
    """The GetResourceAvailability answer: per architecture, never about another one."""

    def _ask(self, tags, served=("linux/amd64",)):
        request = celaut.ArchitectureResources()
        request.architecture.tags.extend(tags)
        with patch.object(ra, "get_resource_availability", return_value={"can_execute": True}) as gra:
            answer = ra.get_architecture_availability(request, served=list(served))
        return answer, gra

    def test_a_served_architecture_is_answered_for_that_architecture(self):
        answer, gra = self._ask(["amd64"])
        self.assertTrue(answer["can_execute"])
        self.assertEqual(gra.call_args.kwargs["arch"], "linux/amd64")

    def test_an_unserved_architecture_is_a_no_with_the_reason(self):
        answer, gra = self._ask(["linux/arm64"])
        self.assertFalse(answer["can_execute"])
        self.assertIn("does not run architecture linux/arm64", answer["reason"])
        gra.assert_not_called()

    def test_no_architecture_is_the_native_one(self):
        answer, gra = self._ask([])
        self.assertTrue(answer["can_execute"])
        self.assertIsNone(gra.call_args.kwargs["arch"])


class DelegationCarryThroughTests(unittest.TestCase):
    """The requirement reaches the peer: no forwarding path rebuilds a Sysresources."""

    def test_a_workload_group_is_put_to_a_peer_with_its_benchmark(self):
        service = celaut.Service()
        workload = service.possible_environment_workload.add().workloads.add()
        workload.count = 1
        workload.resources.mem_limit = 111
        keyvalue.set_value(workload.resources.benchmark, "int_ops_per_sec", 500000)
        keyvalue.set_value(workload.resources.benchmark, "unknown_primitive", 4)

        asked = []

        def _peer(peer_id, request):
            asked.append((peer_id, request))
            return True

        with patch.object(wa, "_local_resource_availability", return_value={"can_execute": False}), \
                patch.object(wa, "check_resource_availability_on_peer", side_effect=_peer), \
                patch("src.utils.utils.peers_id_iterator", side_effect=lambda **_: iter(["peer-a"])), \
                patch.object(wa.env_manager, "get", side_effect=lambda key, default=None: default):
            wa.evaluate_possible_environment_workloads(service, None)

        self.assertTrue(asked)
        for peer_id, request in asked:
            self.assertEqual(peer_id, "peer-a")
            # A group's benchmark is a minimum, and the request is a single Sysresources,
            # so it travels next to the limits with nothing else to read it as.
            self.assertEqual(
                keyvalue.to_dict(request.resources.benchmark),
                {"int_ops_per_sec": 500000, "unknown_primitive": 4},
            )
            self.assertEqual(request.resources.mem_limit, 111)
            self.assertEqual(list(request.architecture.tags), [])

    def test_a_group_with_an_embedded_dependency_asks_for_its_architecture(self):
        workload = celaut.Service.PossibleEnvironmentWorkload.Workload(count=1)
        workload.resources.mem_limit = 1
        workload.dependency.service.container.architecture.tags.append("linux/arm64")
        self.assertEqual(list(wa.group_request(workload).architecture.tags), ["linux/arm64"])

    def test_the_get_resource_availability_call_sends_the_message_whole(self):
        from src.utils.bee_client import BeeClient

        request = celaut.ArchitectureResources(
            resources=ar_ask(_resources(flt_ops_per_sec=77, unknown_primitive=4))
        )
        request.architecture.tags.append("linux/amd64")
        with patch.object(BeeClient, "call_one", return_value=None) as call_one, \
                patch("src.utils.bee_client.celaut_pb2_grpc.GatewayStub"):
            BeeClient.get_resource_availability(object(), request, client_id="client-1")
            BeeClient.get_resource_availability(object(), request)

        with_client, without_client = (call.kwargs["input"] for call in call_one.call_args_list)
        for sent in (with_client[0], without_client):
            on_the_peer = celaut.ArchitectureResources()
            on_the_peer.ParseFromString(sent.SerializeToString())
            self.assertEqual(list(on_the_peer.architecture.tags), ["linux/amd64"])
            self.assertEqual(
                keyvalue.to_dict(on_the_peer.resources.benchmark),
                {"flt_ops_per_sec": 77, "unknown_primitive": 4},
            )

    def test_a_service_spec_keeps_it_through_a_node_that_forwards_it(self):
        # Delegation ships the service's own bytes; a node in the middle that parses
        # and re-serializes the spec hands the next peer the same requirement.
        service = celaut.Service()
        keyvalue.set_value(service.container.resources.at_init.benchmark, "int_ops_per_sec", 100)
        keyvalue.set_value(service.container.resources.at_most.benchmark, "int_ops_per_sec", 100)
        keyvalue.set_value(service.container.resources.at_most.benchmark, "unknown_primitive", 4)

        relayed = celaut.Service()
        relayed.ParseFromString(service.SerializeToString())
        forwarded = celaut.Service()
        forwarded.ParseFromString(relayed.SerializeToString())

        self.assertEqual(forwarded.container.resources, service.container.resources)
        self.assertEqual(
            keyvalue.to_dict(forwarded.container.resources.at_most.benchmark),
            {"int_ops_per_sec": 100, "unknown_primitive": 4},
        )


if __name__ == "__main__":
    unittest.main()
