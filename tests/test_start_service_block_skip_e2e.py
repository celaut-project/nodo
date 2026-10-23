"""End-to-end block-skip test for the Service that a request carries: real gRPC server,
real bee_rpc framing.

``test_get_service_block_skip_e2e.py`` is the same test for the other direction. Here the
node receives the Service (``StartService`` and ``GetServiceEstimatedCost``, through
``AbstractInputServiceIterable``), and the node that holds a block says so to the sender
in the response direction (issue #371). Before, it read the whole block from the wire and
dropped it.

The test counts the bytes that the request carried, not only that the call worked: a
skip that never reaches the sender is a call that works, only slower.
"""
import os
import shutil
import tempfile
import unittest
from concurrent import futures
from unittest.mock import patch

IMPORT_ERROR = None
try:
    import grpc
    from bee_rpc import block_builder
    from bee_rpc.client import client_grpc
    from bee_rpc.utils import Enviroment, modify_env, block_pointer

    from protos import celaut_pb2 as celaut
    from protos import celaut_pb2_grpc
    from protos.gateway_bee import rpc_input, rpc_output

    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.identity.node_identity import get_node_public_key_hex
    from src.identity.grpc_transport import node_channel, server_credentials
    from src.gateway.gateway import Gateway
    from src.gateway.iterables.abstract_input_service_iterable import AbstractInputServiceIterable
    from src.utils.bee_client import BeeClient, Dir
    from src.utils.hashing import get_configured_hash_id
    import src.gateway.utils as gateway_utils
    import src.gateway.iterables.abstract_input_service_iterable as abstract_input_service_iterable
    import src.utils.utils as nodo_utils
    import src.packers.zip_with_dockerfile as packer
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    grpc = None  # type: ignore[assignment]

RPC_TIMEOUT_S = 20.0
# Large enough that "the block was sent" and "it was not" are not the same order of
# magnitude as the control messages of a call, and that the sender is still in the block
# when the skip request arrives.
SHARED_BLOCK_SIZE = 40_000_000


class _ProbeIterable(AbstractInputServiceIterable):
    """The real input side of ``StartService``, with an answer that starts nothing."""

    def generate(self):
        yield from BeeClient.respond(
            indices=rpc_output("StartService"),
            message_iterator=iter([celaut.ServiceInstance(token="probe")]),
        )


class _ProbeGateway(Gateway):
    def StartService(self, request_iterator, context, **kwargs):
        yield from _ProbeIterable(request_iterator, context)


class _RequestBytesInterceptor(grpc.StreamStreamClientInterceptor if grpc else object):
    """Sums the wire size of every request `Buffer` that a call sends."""

    def __init__(self):
        self.request_bytes = 0

    def intercept_stream_stream(self, continuation, client_call_details, request_iterator):
        def counting():
            for request in request_iterator:
                self.request_bytes += request.ByteSize()
                yield request

        return continuation(client_call_details, counting())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StartServiceBlockSkipEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="start-service-skip-")
        self.addCleanup(shutil.rmtree, self.root, True)

        self.blocks = os.path.join(self.root, "blocks") + os.sep
        self.cache = os.path.join(self.root, "cache") + os.sep
        os.makedirs(self.blocks)
        os.makedirs(self.cache)
        modify_env(cache_dir=self.cache, block_dir=self.blocks)
        self.addCleanup(modify_env, cache_dir=packer.CACHE, block_dir=packer.BLOCKDIR)

        self.registry = os.path.join(self.root, "registry") + os.sep
        self.metadata_registry = os.path.join(self.root, "metadata") + os.sep
        os.makedirs(self.registry)
        os.makedirs(self.metadata_registry)
        for module in (gateway_utils, abstract_input_service_iterable, nodo_utils):
            original_registry, original_metadata = module.REGISTRY, module.METADATA_REGISTRY
            self.addCleanup(setattr, module, "REGISTRY", original_registry)
            self.addCleanup(setattr, module, "METADATA_REGISTRY", original_metadata)
            module.REGISTRY = self.registry
            module.METADATA_REGISTRY = self.metadata_registry

        self.hash_id = get_configured_hash_id()

        local_instance_patcher = patch(
            "src.gateway.client_gate.get_internal_service_id_by_uri", return_value="self"
        )
        local_instance_patcher.start()
        self.addCleanup(local_instance_patcher.stop)

        self.grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        celaut_pb2_grpc.add_GatewayServicer_to_server(_ProbeGateway(), self.grpc_server)
        gateway_port = self.grpc_server.add_secure_port("127.0.0.1:0", server_credentials())
        self.grpc_server.start()
        self.addCleanup(self.grpc_server.stop, None)
        self.gateway = f"127.0.0.1:{gateway_port}"
        self.peer_id = get_node_public_key_hex()

        # The node holds the block, and has not got the service. One process and one
        # block directory stand for the two nodes (as in the GetService test): what
        # differs between the calls below is only whether the sender is told.
        big_path = os.path.join(self.root, "big.bin")
        with open(big_path, "wb") as f:
            f.write(os.urandom(SHARED_BLOCK_SIZE))
        big_hash, _ = block_builder.create_block(file_path=big_path, copy=True)
        os.remove(big_path)

        service = celaut.Service()
        service.container.filesystem = block_pointer(block_id=big_hash).SerializeToString()
        service.container.init.entry_path.append("/entry")
        _, self.service_dir = block_builder.build_multiblock(
            pf_object_with_block_pointers=service,
            blocks=[big_hash],
        )
        self.service_hash = "aa" * 20
        self.metadata = celaut.Metadata(
            hashtag=celaut.Metadata.HashTag(
                hash=[celaut.Metadata.HashTag.Hash(
                    type=self.hash_id, value=bytes.fromhex(self.service_hash)
                )]
            )
        )

    def _request(self):
        """What ``service_extended`` sends, with the Service from the cache of the test."""
        yield celaut.Client(client_id="test-fixture")
        yield from self.metadata.hashtag.hash
        yield self.metadata
        yield Dir(dir=self.service_dir, _type=celaut.Service)

    def _start(self, *, block_skip: bool) -> tuple:
        channel = node_channel(self.gateway, expected_peer_id=self.peer_id)
        self.addCleanup(channel.close)
        counter = _RequestBytesInterceptor()
        stub = celaut_pb2_grpc.GatewayStub(grpc.intercept_channel(channel, counter))
        instance = next(client_grpc(
            method=stub.StartService,
            indices_serializer=rpc_input("StartService"),
            input=self._request(),
            indices_parser=rpc_output("StartService"),
            partitions_message_mode_parser=True,
            block_skip=block_skip,
            timeout=RPC_TIMEOUT_S,
        ))
        # Each call is a new service for the node.
        shutil.rmtree(os.path.join(self.registry, self.service_hash), ignore_errors=True)
        return instance, counter.request_bytes

    def test_a_block_the_node_holds_is_not_sent(self):
        # The sender does not ask for skips: the node says which blocks it holds, and
        # the sender goes on. The answer is the same, and the whole block is sent.
        instance, baseline = self._start(block_skip=False)
        self.assertEqual(instance.token, "probe")
        self.assertGreaterEqual(
            baseline, SHARED_BLOCK_SIZE,
            f"the test does not send the block: {baseline} bytes for {SHARED_BLOCK_SIZE}"
        )

        # A sender that asks. The request of the skip is a race with the sender, so it
        # has some tries, as in the GetService test: a skip that never works fails all.
        attempts = []
        for _ in range(5):
            instance, sent = self._start(block_skip=True)
            self.assertEqual(instance.token, "probe")
            attempts.append(sent)
            if sent < SHARED_BLOCK_SIZE // 2:
                break
        self.assertLess(
            min(attempts), SHARED_BLOCK_SIZE // 2,
            f"the node held the block and the sender sent it anyway: {attempts} bytes "
            f"for a {SHARED_BLOCK_SIZE}-byte block, {baseline} bytes without the skip"
        )

    def test_nodo_asks_for_the_skip_in_its_own_calls(self):
        channel = node_channel(self.gateway, expected_peer_id=self.peer_id)
        self.addCleanup(channel.close)
        counter = _RequestBytesInterceptor()
        channel = grpc.intercept_channel(channel, counter)

        attempts = []
        for _ in range(5):
            counter.request_bytes = 0
            instance = BeeClient.start_service(channel, self._request(), timeout=RPC_TIMEOUT_S)
            shutil.rmtree(os.path.join(self.registry, self.service_hash), ignore_errors=True)
            self.assertEqual(instance.token, "probe")
            attempts.append(counter.request_bytes)
            if counter.request_bytes < SHARED_BLOCK_SIZE // 2:
                break
        self.assertLess(min(attempts), SHARED_BLOCK_SIZE // 2, f"{attempts}")


if __name__ == "__main__":
    unittest.main()
