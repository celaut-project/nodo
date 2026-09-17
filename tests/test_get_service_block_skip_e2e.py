"""End-to-end block-skip test for `GetService`: real gRPC server, real bee_rpc framing.

``test_service_tunnel_e2e.py`` is the precedent for testing a seam like this one for
real rather than through a mock: the parts that broke historically are the ones no
unit test of either side alone can exercise.

Issue #371: a receiver that already held a block used to get it pushed at it in full
over the wire and drop it on the floor -- deduplication saved disk, never bandwidth.
`bee_rpc.control.StreamControl` is the channel that lets the receiver say "I already
have this one" mid-stream, and the sender honour it; `client_grpc(..., block_skip=True)`
and `GetServiceIterable`'s own `StreamControl` are what nodo now wires it through. This
asserts the number of bytes that actually crossed the wire, not just that nothing broke
-- "everything still works, only slower" is exactly the failure mode a behavioural test
would miss.
"""
import os
import shutil
import tempfile
import unittest
from concurrent import futures

IMPORT_ERROR = None
try:
    import grpc
    from bee_rpc import block_builder
    from bee_rpc.client import client_grpc
    from bee_rpc.utils import Enviroment, modify_env, block_pointer

    from protos import celaut_pb2 as celaut
    from protos import celaut_pb2_grpc
    from protos.gateway_bee import StartService_input_indices, StartService_input_message_mode

    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.identity.node_identity import get_node_public_key_hex
    from src.identity.grpc_transport import node_channel, server_credentials
    from src.gateway.gateway import Gateway
    from src.utils.hashing import get_configured_hash_id
    import src.gateway.utils as gateway_utils
    import src.gateway.iterables.abstract_input_service_iterable as abstract_input_service_iterable
    import src.utils.utils as nodo_utils
    import src.packers.zip_with_dockerfile as packer
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    grpc = None  # type: ignore[assignment]

RPC_TIMEOUT_S = 10.0
# Comfortably over any chunking/inlining threshold, and large enough that "the
# block was resent" and "it wasn't" are not the same order of magnitude as the
# handful of small control messages a call exchanges either way.
SHARED_BLOCK_SIZE = 300_000


class _ByteCountingInterceptor(grpc.StreamStreamClientInterceptor if grpc else object):
    """Sums the wire size of every response `Buffer` a call actually receives."""

    def __init__(self):
        self.response_bytes = 0
        self.response_count = 0

    def intercept_stream_stream(self, continuation, client_call_details, request_iterator):
        call = continuation(client_call_details, request_iterator)

        def counting():
            for response in call:
                self.response_bytes += response.ByteSize()
                self.response_count += 1
                yield response

        return counting()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GetServiceBlockSkipEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="get-service-skip-")
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
        # Three modules each bound their own module-level REGISTRY/METADATA_REGISTRY
        # from config at import time (find_service_hash, service_extended,
        # save_service all read their own copy) -- every one has to be redirected,
        # or the servicer finds nothing under the hash this test registers.
        for module in (gateway_utils, abstract_input_service_iterable, nodo_utils):
            original_registry, original_metadata = module.REGISTRY, module.METADATA_REGISTRY
            self.addCleanup(setattr, module, "REGISTRY", original_registry)
            self.addCleanup(setattr, module, "METADATA_REGISTRY", original_metadata)
            module.REGISTRY = self.registry
            module.METADATA_REGISTRY = self.metadata_registry

        self.hash_id = get_configured_hash_id()

        self.grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        celaut_pb2_grpc.add_GatewayServicer_to_server(Gateway(), self.grpc_server)
        gateway_port = self.grpc_server.add_secure_port("127.0.0.1:0", server_credentials())
        self.grpc_server.start()
        self.addCleanup(self.grpc_server.stop, None)
        self.gateway = f"127.0.0.1:{gateway_port}"
        self.peer_id = get_node_public_key_hex()

    def _channel(self, interceptor=None) -> grpc.Channel:
        channel = node_channel(self.gateway, expected_peer_id=self.peer_id)
        self.addCleanup(channel.close)
        return grpc.intercept_channel(channel, interceptor) if interceptor else channel

    def _register_service(self, service_hash: str, big_block_hash: bytes, entry: str) -> None:
        """A minimal service whose container references `big_block_hash`, on disk
        exactly where `GetService` looks for one: a multiblock directory under
        `REGISTRY`, with the per-file pointer to the shared block still in it
        (not expanded -- see `container_filesystem.py` for why that distinction
        matters when the tree is read back)."""
        service = celaut.Service()
        service.container.filesystem = block_pointer(block_id=big_block_hash).SerializeToString()
        service.container.init.entry_path.append(entry)
        _, cache_dir = block_builder.build_multiblock(
            pf_object_with_block_pointers=service,
            blocks=[big_block_hash],
        )
        metadata = celaut.Metadata(
            hashtag=celaut.Metadata.HashTag(
                hash=[celaut.Metadata.HashTag.Hash(type=self.hash_id, value=bytes.fromhex(service_hash))]
            )
        )
        self.assertTrue(
            gateway_utils.save_service(metadata=metadata, service_dir=cache_dir, service_hash=service_hash)
        )

    def _fetch(self, service_hash: str, *, block_skip: bool, interceptor=None) -> list:
        stub = celaut_pb2_grpc.GatewayStub(self._channel(interceptor))
        _hash = celaut.Metadata.HashTag.Hash(type=self.hash_id, value=bytes.fromhex(service_hash))
        return list(
            client_grpc(
                method=stub.GetService,
                indices_serializer=celaut.Metadata.HashTag.Hash,
                input=_hash,
                indices_parser=StartService_input_indices,
                partitions_message_mode_parser=StartService_input_message_mode,
                block_skip=block_skip,
                timeout=RPC_TIMEOUT_S,
            )
        )

    def test_a_block_the_receiver_already_holds_is_not_resent(self):
        big_path = os.path.join(self.root, "big.bin")
        with open(big_path, "wb") as f:
            f.write(os.urandom(SHARED_BLOCK_SIZE))
        # Creating it here is what "the receiver already has this content" means in
        # this test: client and server share one process and one block directory,
        # the same way two real nodes each already hold a block they built or
        # fetched independently -- what differs between the two fetches below is
        # only whether the *call* is told to say so.
        big_hash, _ = block_builder.create_block(file_path=big_path, copy=True)

        service_hash = "aa" * 20
        self._register_service(service_hash, big_hash, "/entry")

        # Baseline: block-skip off. This is issue #371's actual bug, reproduced --
        # the block is on both ends already, and the full body crosses the wire
        # anyway, because nothing on either side says otherwise.
        baseline = _ByteCountingInterceptor()
        without_skip = self._fetch(service_hash, block_skip=False, interceptor=baseline)
        self.assertTrue(any(hasattr(item, "dir") for item in without_skip))
        self.assertGreaterEqual(
            baseline.response_bytes, SHARED_BLOCK_SIZE,
            f"test setup is not exercising the bug: only {baseline.response_bytes} bytes "
            f"arrived for a {SHARED_BLOCK_SIZE}-byte shared block even with skip off"
        )

        # Same service, same shared block, only `block_skip=True` differs.
        skipped = _ByteCountingInterceptor()
        with_skip = self._fetch(service_hash, block_skip=True, interceptor=skipped)
        self.assertTrue(any(hasattr(item, "dir") for item in with_skip))

        self.assertLess(
            skipped.response_bytes, SHARED_BLOCK_SIZE,
            f"the shared block was resent: {skipped.response_bytes} bytes on the wire "
            f"for a {SHARED_BLOCK_SIZE}-byte shared block, no smaller than the "
            f"{baseline.response_bytes}-byte baseline with skip off"
        )


if __name__ == "__main__":
    unittest.main()
