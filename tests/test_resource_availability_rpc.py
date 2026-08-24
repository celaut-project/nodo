"""End-to-end test of the GetResourceAvailability wire path.

The two unit-test files around this one both mock the transport away:
`test_workload_admission` patches `check_resource_availability_on_peer`, and
`test_resource_availability_rpc_spec` only inspects the proto descriptor. Neither runs a
byte through it -- and the serialization pairing (the iterable's `serialize_to_buffer`
against the client's `indices_parser` + `partitions_message_mode_parser`) is exactly the
kind of thing that is silently wrong until a real peer is asked.

So this serves the real `GetResourceAvailabilityIterable` over a real grpc server and
calls it with the real `check_resource_availability_on_peer`, with only the *address
lookup* patched. Requires bee_rpc and grpc; skipped where they are not installed.
"""
import unittest
from concurrent import futures
from unittest.mock import patch

try:
    import grpc
    from bee_rpc import client as bee  # noqa: F401
    # Imported, not just checked: `check_resource_availability_on_peer` resolves
    # `generate_uris_by_peer_id` out of this module at call time, so patching it
    # requires the module object to exist.
    import src.utils.utils  # noqa: F401
    _MISSING = None
except ImportError as exc:  # pragma: no cover - environment-dependent
    _MISSING = str(exc)

from protos import celaut_pb2 as celaut

# bee_rpc reads `FieldDescriptor.label`, which protobuf dropped in 7.x. Where the
# installed protobuf is newer than bee_rpc supports, nothing that serializes a Buffer
# can run -- including the node itself -- so this is an environment mismatch to skip
# over, not a failure of the code under test.
if _MISSING is None and not hasattr(celaut.ResourceAvailability.DESCRIPTOR.fields[0], "label"):
    _MISSING = "installed protobuf is newer than bee_rpc supports (no FieldDescriptor.label)"


@unittest.skipIf(_MISSING, f"needs a working grpc/bee_rpc ({_MISSING})")
class ResourceAvailabilityRoundTripTests(unittest.TestCase):
    """One real server, one real client, one real Buffer stream between them."""

    ANSWERS = {}

    @classmethod
    def setUpClass(cls):
        from protos import celaut_pb2_grpc
        from src.gateway.iterables.resource_availability_iterable import (
            GetResourceAvailabilityIterable,
        )

        answers = cls.ANSWERS
        received = cls.RECEIVED = []

        # The iterable under test, wired to a stand-in for the local admission gate so
        # the test controls the answer without depending on the host's real memory.
        class _Servicer(celaut_pb2_grpc.Gateway):
            def GetResourceAvailability(self, request_iterator, context, **kwargs):
                with patch(
                    "src.gateway.iterables.resource_availability_iterable.get_resource_availability",
                    side_effect=lambda resources: (
                        received.append(resources) or answers
                    ),
                ):
                    yield from GetResourceAvailabilityIterable(request_iterator, context)

        cls.server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        celaut_pb2_grpc.add_GatewayServicer_to_server(_Servicer(), cls.server)
        cls.port = cls.server.add_insecure_port("127.0.0.1:0")
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop(None)

    def _ask(self, resources):
        from src.utils.cost_functions.workload_admission import (
            check_resource_availability_on_peer,
        )
        with patch(
            "src.utils.utils.generate_uris_by_peer_id",
            side_effect=lambda peer_id: iter([f"127.0.0.1:{self.port}"]),
        ):
            return check_resource_availability_on_peer("a-peer", resources)

    def setUp(self):
        self.RECEIVED.clear()

    def test_a_yes_survives_the_round_trip(self):
        self.ANSWERS.clear()
        self.ANSWERS.update({"can_execute": True, "reason": ""})
        self.assertIs(self._ask(celaut.Service.Container.Resources()), True)

    def test_a_no_survives_the_round_trip(self):
        self.ANSWERS.clear()
        self.ANSWERS.update({"can_execute": False, "reason": "not enough memory"})
        self.assertIs(self._ask(celaut.Service.Container.Resources()), False)

    def test_the_declared_resources_arrive_intact(self):
        self.ANSWERS.clear()
        self.ANSWERS.update({"can_execute": True, "reason": ""})
        asked = celaut.Service.Container.Resources(
            at_most=celaut.Sysresources(
                mem_limit=4 * 1024 ** 3, disk_space=40 * 1024 ** 3,
                cpu_quota=200000, cpu_period=100000, blkio_weight=500,
            )
        )
        self._ask(asked)
        self.assertEqual(len(self.RECEIVED), 1)
        # Not just "a Resources arrived" -- the same one, field for field. A partitioning
        # mismatch between the two sides shows up here as a truncated or empty message.
        self.assertEqual(self.RECEIVED[0], asked)

    def test_an_unreachable_peer_is_not_a_no(self):
        # None means "could not ask", which _workload_group_is_satisfiable must not read
        # as a refusal. A closed port is the cheapest way to produce it.
        from src.utils.cost_functions.workload_admission import (
            check_resource_availability_on_peer,
        )
        with patch(
            "src.utils.utils.generate_uris_by_peer_id",
            side_effect=lambda peer_id: iter(["127.0.0.1:1"]),
        ):
            answer = check_resource_availability_on_peer(
                "a-peer", celaut.Service.Container.Resources()
            )
        self.assertIsNone(answer)


if __name__ == "__main__":
    unittest.main()
