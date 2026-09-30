"""`Sysresources.min_benchmark` (#448): the per-core minimum benchmark a service requires.

Four things are pinned here, one class each:

* **the wire** -- the map round-trips, an unrecognised key included, and a node built
  before the field existed still reads every other field of a message that carries it;
* **service.json** -- what the packer accepts, what it refuses, and that a service that
  never mentions the field serializes to exactly what it did before;
* **admission today** -- the requirement is read and logged, never a reason to refuse,
  because no node measures its cores yet;
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
from src.utils import min_benchmark  # noqa: E402
from src.utils.cost_functions import resource_availability as ra  # noqa: E402
from src.utils.cost_functions import workload_admission as wa  # noqa: E402
from src.utils.min_benchmark import MIN_BENCHMARK_KEYS, parse_min_benchmark  # noqa: E402

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

    def test_the_vocabulary_is_the_four_named_primitives(self):
        self.assertEqual(
            MIN_BENCHMARK_KEYS,
            (
                "int_ops_per_sec",
                "flt_ops_per_sec",
                "mem_bandwidth_bytes_per_sec",
                "sha256_hashes_per_sec",
            ),
        )

    def test_it_is_field_six_a_string_to_uint64_map(self):
        field = celaut.Sysresources.DESCRIPTOR.fields_by_name["min_benchmark"]
        self.assertEqual(field.number, 6)
        entry = field.message_type
        self.assertTrue(entry.GetOptions().map_entry)
        self.assertEqual(entry.fields_by_name["key"].type, entry.fields_by_name["key"].TYPE_STRING)
        self.assertEqual(entry.fields_by_name["value"].type, entry.fields_by_name["value"].TYPE_UINT64)

    def test_round_trip_keeps_every_key_recognised_or_not(self):
        sent = celaut.Sysresources(cpu_quota=200000, cpu_period=100000)
        sent.min_benchmark["int_ops_per_sec"] = 500000
        sent.min_benchmark["sha256_hashes_per_sec"] = 2 ** 64 - 1
        sent.min_benchmark["a_primitive_from_the_future"] = 7

        received = celaut.Sysresources()
        received.ParseFromString(sent.SerializeToString())

        self.assertEqual(
            dict(received.min_benchmark),
            {
                "int_ops_per_sec": 500000,
                "sha256_hashes_per_sec": 2 ** 64 - 1,
                "a_primitive_from_the_future": 7,
            },
        )
        self.assertEqual(received, sent)
        self.assertEqual(
            min_benchmark.unrecognised_keys(received.min_benchmark),
            ("a_primitive_from_the_future",),
        )

    def test_an_absent_key_is_absent_not_zero(self):
        sysreq = celaut.Sysresources()
        sysreq.min_benchmark["int_ops_per_sec"] = 1
        self.assertNotIn("flt_ops_per_sec", sysreq.min_benchmark)
        # A bare read of a map inserts the default; `in` and `.get` are the readers.
        self.assertIsNone(sysreq.min_benchmark.get("flt_ops_per_sec"))

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
        sent.min_benchmark["int_ops_per_sec"] = 500000
        sent.min_benchmark["unknown_primitive"] = 9

        old = _pre_448_sysresources()()
        old.ParseFromString(sent.SerializeToString())

        self.assertEqual(
            (old.blkio_weight, old.cpu_period, old.cpu_quota, old.mem_limit, old.disk_space),
            (500, 100000, 200000, 1024, 2048),
        )
        self.assertFalse(hasattr(old, "min_benchmark"))

    def test_a_pre_448_relay_does_not_strip_it(self):
        # An old node that parses and re-serializes keeps field 6 as unknown bytes, so
        # a requirement survives being forwarded through a peer that cannot read it.
        sent = celaut.Sysresources(mem_limit=1024)
        sent.min_benchmark["flt_ops_per_sec"] = 42

        old = _pre_448_sysresources()()
        old.ParseFromString(sent.SerializeToString())
        received = celaut.Sysresources()
        received.ParseFromString(old.SerializeToString())

        self.assertEqual(dict(received.min_benchmark), {"flt_ops_per_sec": 42})
        self.assertEqual(received.mem_limit, 1024)

    def test_a_pre_448_message_reads_as_no_requirement(self):
        old = _pre_448_sysresources()(mem_limit=1024, cpu_quota=100000)
        received = celaut.Sysresources()
        received.ParseFromString(old.SerializeToString())
        self.assertEqual(dict(received.min_benchmark), {})
        self.assertEqual(received.mem_limit, 1024)

    def test_it_survives_the_pack_schema_to_celaut_schema_reread(self):
        # ZipContainerPacker.save re-reads the pack.Service bytes as a celaut.Service.
        packed = pack_pb2.Service()
        packed.container.resources.at_most.min_benchmark["int_ops_per_sec"] = 5
        spec = celaut.Service()
        spec.ParseFromString(packed.SerializeToString())
        self.assertEqual(dict(spec.container.resources.at_most.min_benchmark), {"int_ops_per_sec": 5})


class ParseMinBenchmarkTests(unittest.TestCase):

    def test_absent_is_no_requirement(self):
        self.assertEqual(parse_min_benchmark(None, "resources.at_most.min_benchmark"), {})
        self.assertEqual(parse_min_benchmark({}, "resources.at_most.min_benchmark"), {})

    def test_valid_values_are_kept_sorted_by_key(self):
        parsed = parse_min_benchmark(
            {"sha256_hashes_per_sec": 1000000, "int_ops_per_sec": 500000, "flt_ops_per_sec": 0},
            "resources.at_most.min_benchmark",
        )
        self.assertEqual(
            list(parsed.items()),
            [("flt_ops_per_sec", 0), ("int_ops_per_sec", 500000), ("sha256_hashes_per_sec", 1000000)],
        )

    def test_an_unrecognised_key_is_kept_not_refused(self):
        self.assertEqual(
            parse_min_benchmark({"gpu_matmul_per_sec": 3}, "resources.at_most.min_benchmark"),
            {"gpu_matmul_per_sec": 3},
        )

    def test_the_largest_uint64_is_accepted(self):
        self.assertEqual(
            parse_min_benchmark({"int_ops_per_sec": 2 ** 64 - 1}, "p"), {"int_ops_per_sec": 2 ** 64 - 1}
        )

    def test_anything_but_a_non_negative_integer_is_refused_naming_the_key(self):
        for bad in (-1, 1.5, 500000.0, "500000", True, False, None, [1], {"n": 1}, 2 ** 64):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as raised:
                    parse_min_benchmark(
                        {"int_ops_per_sec": bad}, "resources.at_most.min_benchmark"
                    )
                self.assertIn(
                    "resources.at_most.min_benchmark.int_ops_per_sec", str(raised.exception)
                )

    def test_anything_but_an_object_is_refused(self):
        for bad in (500000, "int_ops_per_sec", ["int_ops_per_sec"], True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as raised:
                    parse_min_benchmark(bad, "resources.at_init.min_benchmark")
                self.assertIn("resources.at_init.min_benchmark must be an object", str(raised.exception))

    def test_an_empty_key_is_refused(self):
        with self.assertRaises(ValueError):
            parse_min_benchmark({"": 1}, "resources.at_most.min_benchmark")


def _packer(service_json):
    """A ZipContainerPacker carrying only the json `_min_benchmarks` reads.

    __init__ drives BuildKit; this method reads `self.json` and nothing else.
    """
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = service_json
    return packer


@unittest.skipIf(
    PACKER_IMPORT_ERROR is not None, f"Missing runtime dependencies: {PACKER_IMPORT_ERROR}"
)
class PackerServiceJsonTests(unittest.TestCase):

    def test_absent_everywhere_is_two_empty_maps(self):
        for service_json in ({}, {"resources": {}}, {"resources": {"at_init": {}, "at_most": {}}}):
            with self.subTest(service_json=service_json):
                self.assertEqual(_packer(service_json)._min_benchmarks(), ({}, {}))

    def test_each_end_is_read_from_its_own_object(self):
        at_init, at_most = _packer({"resources": {
            "at_init": {"min_benchmark": {"int_ops_per_sec": 100}},
            "at_most": {"min_benchmark": {"int_ops_per_sec": 300, "flt_ops_per_sec": 50}},
        }})._min_benchmarks()
        self.assertEqual(at_init, {"int_ops_per_sec": 100})
        self.assertEqual(at_most, {"flt_ops_per_sec": 50, "int_ops_per_sec": 300})

    def test_at_most_is_raised_to_at_init_key_by_key(self):
        # Admission reads at_most: a minimum written only under at_init must not be
        # one no node ever looks at, and at_most must never ask for less than at_init.
        at_init, at_most = _packer({"resources": {
            "at_init": {"min_benchmark": {"int_ops_per_sec": 500, "sha256_hashes_per_sec": 9}},
            "at_most": {"min_benchmark": {"int_ops_per_sec": 100}},
        }})._min_benchmarks()
        self.assertEqual(at_init, {"int_ops_per_sec": 500, "sha256_hashes_per_sec": 9})
        self.assertEqual(at_most, {"int_ops_per_sec": 500, "sha256_hashes_per_sec": 9})

    def test_a_malformed_value_is_refused_at_either_end(self):
        for end in ("at_init", "at_most"):
            for bad in (-5, 2.5, "9"):
                with self.subTest(end=end, value=bad):
                    with self.assertRaises(ValueError) as raised:
                        _packer({"resources": {end: {"min_benchmark": {"flt_ops_per_sec": bad}}}})._min_benchmarks()
                    self.assertIn(f"resources.{end}.min_benchmark.flt_ops_per_sec", str(raised.exception))

    def test_it_is_refused_when_service_json_is_read_not_after_the_build(self):
        packer = _packer({"resources": {"at_most": {"min_benchmark": {"int_ops_per_sec": -1}}}})
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
            {"mem_limit": 100, "min_benchmark": {"int_ops_per_sec": 500000, "unknown_primitive": 1}}
        )
        self.assertEqual(
            dict(resources.min_benchmark), {"int_ops_per_sec": 500000, "unknown_primitive": 1}
        )
        self.assertEqual(resources.mem_limit, 100)

    def test_a_workload_group_without_it_has_none(self):
        self.assertEqual(dict(self._workloads({"mem_limit": 100}).min_benchmark), {})

    def test_a_workload_group_refuses_negative_and_non_integer_values(self):
        for bad in (-1, 1.5, True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self._workloads({"min_benchmark": {"int_ops_per_sec": bad}})

    def test_an_embedded_dependency_service_carries_it(self):
        service = parse_service_spec({"container": {"resources": {
            "at_most": {"min_benchmark": {"mem_bandwidth_bytes_per_sec": 1000}},
        }}})
        self.assertEqual(
            dict(service.container.resources.at_most.min_benchmark),
            {"mem_bandwidth_bytes_per_sec": 1000},
        )

    def test_protobuf_json_round_trip(self):
        sysreq = celaut.Sysresources()
        sysreq.min_benchmark["int_ops_per_sec"] = 5
        again = json_format.ParseDict(json_format.MessageToDict(sysreq), celaut.Sysresources())
        self.assertEqual(again, sysreq)


def _resources(**benchmarks) -> celaut.Service.Container.Resources:
    resources = celaut.Service.Container.Resources(
        at_most=celaut.Sysresources(mem_limit=1024, cpu_quota=100000, cpu_period=100000)
    )
    resources.at_most.min_benchmark.update(benchmarks)
    return resources


class AdmissionTodayTests(unittest.TestCase):
    """Declared, logged, not enforced: nothing on a node measures a core yet."""

    def _availability(self, resources):
        with patch.object(ra, "could_ve_this_sysreq", return_value=True), \
                patch.object(ra.host_limits, "ceiling_shortfalls", return_value=[]), \
                patch.object(ra.log, "LOGGER") as logger:
            return ra.get_resource_availability(resources), [c.args[0] for c in logger.call_args_list]

    def test_an_unreachable_requirement_does_not_refuse_the_service(self):
        availability, _ = self._availability(_resources(int_ops_per_sec=2 ** 64 - 1))
        self.assertTrue(availability["can_execute"])
        self.assertEqual(availability["reason"], "")

    def test_it_changes_nothing_about_the_answer(self):
        with_it, _ = self._availability(_resources(int_ops_per_sec=500000))
        without_it, _ = self._availability(_resources())
        for volatile in ("system_cpu_available_percent", "system_memory_available", "system_disk_free"):
            with_it.pop(volatile), without_it.pop(volatile)
        self.assertEqual(with_it, without_it)

    def test_it_is_said_in_the_log_with_every_declared_primitive(self):
        _, lines = self._availability(_resources(sha256_hashes_per_sec=9, int_ops_per_sec=500000))
        self.assertEqual(len(lines), 1)
        self.assertIn("int_ops_per_sec=500000, sha256_hashes_per_sec=9", lines[0])
        self.assertIn("not enforced", lines[0])
        self.assertNotIn("Unrecognised", lines[0])

    def test_an_unrecognised_primitive_is_named_and_is_not_an_error(self):
        availability, lines = self._availability(_resources(quantum_ops_per_sec=3))
        self.assertTrue(availability["can_execute"])
        self.assertIn("Unrecognised primitive(s): quantum_ops_per_sec.", lines[0])

    def test_no_requirement_no_log_line(self):
        _, lines = self._availability(_resources())
        self.assertEqual(lines, [])

    def test_a_real_shortfall_is_still_a_refusal(self):
        resources = _resources(int_ops_per_sec=1)
        with patch.object(ra, "could_ve_this_sysreq", return_value=False), \
                patch.object(ra.host_limits, "ceiling_shortfalls", return_value=[]), \
                patch.object(ra.log, "LOGGER"):
            self.assertFalse(ra.get_resource_availability(resources)["can_execute"])


class DelegationCarryThroughTests(unittest.TestCase):
    """The requirement reaches the peer: no forwarding path rebuilds a Sysresources."""

    def test_a_workload_group_is_put_to_a_peer_with_its_min_benchmark(self):
        service = celaut.Service()
        workload = service.possible_environment_workload.add().workloads.add()
        workload.count = 1
        workload.resources.mem_limit = 111
        workload.resources.min_benchmark["int_ops_per_sec"] = 500000
        workload.resources.min_benchmark["unknown_primitive"] = 4

        asked = []

        def _peer(peer_id, resources):
            asked.append((peer_id, resources))
            return True

        with patch.object(wa, "_local_resource_availability", return_value={"can_execute": False}), \
                patch.object(wa, "check_resource_availability_on_peer", side_effect=_peer), \
                patch("src.utils.utils.peers_id_iterator", side_effect=lambda **_: iter(["peer-a"])), \
                patch.object(wa.env_manager, "get", side_effect=lambda key, default=None: default):
            self.assertTrue(wa._workload_group_is_satisfiable(
                celaut.Service.Container.Resources(at_most=workload.resources), None
            ))
            wa.evaluate_possible_environment_workloads(service, None)

        self.assertTrue(asked)
        for peer_id, resources in asked:
            self.assertEqual(peer_id, "peer-a")
            self.assertEqual(
                dict(resources.at_most.min_benchmark),
                {"int_ops_per_sec": 500000, "unknown_primitive": 4},
            )
            self.assertEqual(resources.at_most.mem_limit, 111)

    def test_the_get_resource_availability_call_sends_the_message_whole(self):
        from src.utils.bee_client import BeeClient

        resources = _resources(flt_ops_per_sec=77, unknown_primitive=4)
        with patch.object(BeeClient, "call_one", return_value=None) as call_one, \
                patch("src.utils.bee_client.celaut_pb2_grpc.GatewayStub"):
            BeeClient.get_resource_availability(object(), resources, client_id="client-1")
            BeeClient.get_resource_availability(object(), resources)

        with_client, without_client = (call.kwargs["input"] for call in call_one.call_args_list)
        for sent in (with_client[0], without_client):
            on_the_peer = celaut.Service.Container.Resources()
            on_the_peer.ParseFromString(sent.SerializeToString())
            self.assertEqual(
                dict(on_the_peer.at_most.min_benchmark),
                {"flt_ops_per_sec": 77, "unknown_primitive": 4},
            )

    def test_a_service_spec_keeps_it_through_a_node_that_forwards_it(self):
        # Delegation ships the service's own bytes; a node in the middle that parses
        # and re-serializes the spec hands the next peer the same requirement.
        service = celaut.Service()
        service.container.resources.at_init.min_benchmark["int_ops_per_sec"] = 100
        service.container.resources.at_most.min_benchmark["int_ops_per_sec"] = 100
        service.container.resources.at_most.min_benchmark["unknown_primitive"] = 4

        relayed = celaut.Service()
        relayed.ParseFromString(service.SerializeToString())
        forwarded = celaut.Service()
        forwarded.ParseFromString(relayed.SerializeToString())

        self.assertEqual(forwarded.container.resources, service.container.resources)
        self.assertEqual(
            dict(forwarded.container.resources.at_most.min_benchmark),
            {"int_ops_per_sec": 100, "unknown_primitive": 4},
        )


if __name__ == "__main__":
    unittest.main()
