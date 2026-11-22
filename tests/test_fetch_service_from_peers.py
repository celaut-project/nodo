"""`fetch_service_from_peers`: what a peer sends is stored only if it is the service asked for.

A peer that sends another service, or stalls, loses reputation; one that is merely
down does not (unreachable peers are scored by the refresh, once per outage).

`nodo execute` now asks the peers before the source-application (through
`source_application.acquire_service`) and runs what lands in the registry, so a peer
must not be able to put other bytes under the requested id. The `GetService` call
itself is stood in for; `test_get_service_block_skip_e2e.py` covers it for real.
"""
import os
import shutil
import tempfile
import time
import unittest
from concurrent import futures
from unittest.mock import patch

IMPORT_ERROR = None
try:
    import grpc
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.manager import maintain
    from src.utils.bee_client import Dir
    from src.reputation_system.reasons import Reason
    from src.utils.hashing import get_configured_hash_spec, hash_stream
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    maintain = None  # type: ignore[assignment]


# A specification that is the canonical encoding (docs/CANONICAL_ENCODING.md): the node does
# not store a service that is not one, whatever its hash.
SERVICE = celaut_pb2.Service(prose="the service").SerializeToString() if IMPORT_ERROR is None else b""
# The same service with its fields in another order, which protobuf reads as the same message.
NOT_CANONICAL = (b"\x12\x00" + b"\x0a\x03abc") if IMPORT_ERROR is None else b""


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FetchServiceFromPeersTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fetch-from-peers-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.registry = os.path.join(self.root, "registry") + os.sep
        self.metadata_registry = os.path.join(self.root, "metadata") + os.sep
        os.makedirs(self.registry)
        os.makedirs(self.metadata_registry)

        for name, value in (("REGISTRY", self.registry), ("METADATA_REGISTRY", self.metadata_registry)):
            patcher = patch.object(maintain, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("get_client_id_on_other_peer", "peer_channel"):
            patcher = patch.object(maintain, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The watchdog is a real interceptor on a real channel; its own tests are below.
        self.watchdog = None
        channel = patch.object(maintain, "_get_service_channel", side_effect=lambda peer: (peer, self.watchdog))
        channel.start()
        self.addCleanup(channel.stop)

        penalised = patch.object(maintain, "_get_service_penalised", set())
        penalised.start()
        self.addCleanup(penalised.stop)
        reputation = patch.object(maintain, "_reputation_interface")
        self.reputation = reputation.start().return_value
        self.addCleanup(reputation.stop)

    def _received(self, content: bytes) -> str:
        """A file as `GetService` leaves one in the cache; its configured hash."""
        path = tempfile.mktemp(dir=self.root)
        with open(path, "wb") as f:
            f.write(content)
        self.received_path = path
        return hash_stream([content], get_configured_hash_spec()).hex()

    def _peer_sends(self, *peers):
        responses = {
            peer: sent if isinstance(sent, Exception) else [celaut_pb2.Metadata(), Dir(sent, celaut_pb2.Service)]
            for peer, sent in peers
        }
        iterate = patch.object(maintain, "peers_id_iterator", side_effect=lambda: iter(responses))

        def get_service(channel, *a, **k):
            response = responses[channel]
            if isinstance(response, Exception):
                raise response
            return iter(response)

        return iterate, patch.object(maintain.BeeClient, "get_service", side_effect=get_service)

    def _penalties(self):
        return [
            (c.kwargs["peer_id"], c.kwargs["amount"], c.kwargs["reason"])
            for c in self.reputation.update_peer_reputation.call_args_list
        ]

    def test_a_service_that_hashes_to_the_id_is_stored(self):
        wanted = self._received(SERVICE)
        iterate, get = self._peer_sends(("peer-a", self.received_path))
        with iterate, get:
            self.assertTrue(maintain.fetch_service_from_peers(wanted))

        with open(os.path.join(self.registry, wanted), "rb") as f:
            self.assertEqual(f.read(), SERVICE)
        self.assertTrue(os.path.exists(os.path.join(self.metadata_registry, wanted)))

    def test_a_service_that_is_not_canonical_is_not_stored_and_the_peer_is_not_penalised(self):
        # The id is right: every peer holds these bytes, so no peer can give another
        # encoding of the same service. It is not the fault of the peer.
        wanted = self._received(NOT_CANONICAL)
        iterate, get = self._peer_sends(("peer-a", self.received_path))
        with iterate, get:
            self.assertFalse(maintain.fetch_service_from_peers(wanted))

        self.assertFalse(os.path.exists(os.path.join(self.registry, wanted)))
        self.assertEqual(self._penalties(), [])

    def test_with_warn_a_service_that_is_not_canonical_is_stored(self):
        wanted = self._received(NOT_CANONICAL)
        iterate, get = self._peer_sends(("peer-a", self.received_path))
        real = maintain.service_canonical.env_manager.get
        setting = patch.object(
            maintain.service_canonical.env_manager, "get",
            side_effect=lambda key, default=None: "warn" if key == "misc.CANONICAL_SERVICE"
            else real(key, default))
        with iterate, get, setting:
            self.assertTrue(maintain.fetch_service_from_peers(wanted))

        self.assertTrue(os.path.exists(os.path.join(self.registry, wanted)))

    def test_a_service_that_does_not_hash_to_the_id_is_dropped_and_the_next_peer_asked(self):
        wanted = self._received(SERVICE)
        good = self.received_path
        self._received(b"something else")
        bad = self.received_path

        iterate, get = self._peer_sends(("peer-bad", bad), ("peer-good", good))
        with iterate, get:
            self.assertTrue(maintain.fetch_service_from_peers(wanted))

        self.assertFalse(os.path.exists(bad))
        with open(os.path.join(self.registry, wanted), "rb") as f:
            self.assertEqual(f.read(), SERVICE)

    def test_a_peer_that_sends_another_service_is_penalised_once_per_service(self):
        wanted = self._received(SERVICE)
        for _ in range(2):  # `wanted_services_retry` asks again; the peer still lies.
            self._received(b"something else")
            iterate, get = self._peer_sends(("peer-bad", self.received_path))
            with iterate, get:
                self.assertFalse(maintain.fetch_service_from_peers(wanted))

        self.assertEqual(
            self._penalties(),
            [("peer-bad", maintain.GET_SERVICE_WRONG_HASH_PENALTY, Reason.GET_SERVICE_WRONG_HASH)],
        )

    def test_a_peer_past_the_deadline_is_penalised_and_the_next_peer_asked(self):
        wanted = self._received(SERVICE)
        deadline = grpc.RpcError()
        deadline.code = lambda: grpc.StatusCode.DEADLINE_EXCEEDED
        iterate, get = self._peer_sends(("peer-slow", deadline), ("peer-good", self.received_path))
        with iterate, get:
            self.assertTrue(maintain.fetch_service_from_peers(wanted))

        self.assertEqual(
            self._penalties(),
            [("peer-slow", maintain.GET_SERVICE_TIMEOUT_PENALTY, Reason.GET_SERVICE_TIMED_OUT)],
        )

    def test_a_peer_the_idle_watchdog_cut_off_is_penalised(self):
        wanted = self._received(SERVICE)
        self.watchdog = maintain._IdleWatchdog(30)
        self.watchdog.fired = True
        cancelled = grpc.RpcError()
        cancelled.code = lambda: grpc.StatusCode.CANCELLED
        iterate, get = self._peer_sends(("peer-silent", cancelled))
        with iterate, get:
            self.assertFalse(maintain.fetch_service_from_peers(wanted))

        self.assertEqual(
            self._penalties(),
            [("peer-silent", maintain.GET_SERVICE_TIMEOUT_PENALTY, Reason.GET_SERVICE_TIMED_OUT)],
        )

    def test_a_peer_that_fails_otherwise_is_not_penalised_here(self):
        # Unreachable is `PEER_REFRESH_FAILED`'s to score, once per outage.
        wanted = self._received(SERVICE)
        unavailable = grpc.RpcError()
        unavailable.code = lambda: grpc.StatusCode.UNAVAILABLE
        iterate, get = self._peer_sends(("peer-down", unavailable))
        with iterate, get:
            self.assertFalse(maintain.fetch_service_from_peers(wanted))

        self.assertEqual(self._penalties(), [])

    def test_nothing_is_stored_when_no_peer_sends_the_service(self):
        wanted = self._received(SERVICE)
        self._received(b"something else")
        iterate, get = self._peer_sends(("peer-bad", self.received_path))
        with iterate, get:
            self.assertFalse(maintain.fetch_service_from_peers(wanted))

        self.assertEqual(os.listdir(self.registry), [])
        self.assertEqual(os.listdir(self.metadata_registry), [])

    def test_check_wanted_service_queues_a_retry_only_on_failure(self):
        with patch.object(maintain, "fetch_service_from_peers", return_value=False), \
                patch.object(maintain, "wanted_services_retry", set()) as retry:
            maintain.check_wanted_service("ab")
        self.assertEqual(retry, {"ab"})

        with patch.object(maintain, "fetch_service_from_peers", return_value=True), \
                patch.object(maintain, "wanted_services_retry", set()) as retry:
            maintain.check_wanted_service("ab")
        self.assertEqual(retry, set())


def _stream_handler(sends: int, every_s: float):
    """A stream-stream method that sends ``sends`` messages, ``every_s`` seconds apart."""
    def handler(request_iterator, context):
        for _ in range(sends):
            time.sleep(every_s)
            yield b"x"
    return grpc.method_handlers_generic_handler(
        "test.Stream", {"Call": grpc.stream_stream_rpc_method_handler(handler)}
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class IdleWatchdogTests(unittest.TestCase):
    def _call(self, handler, watchdog):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        server.add_generic_rpc_handlers((handler,))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, None)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        call = grpc.intercept_channel(channel, watchdog).stream_stream("/test.Stream/Call")
        return list(call(iter([b"go"])))

    def test_a_silent_peer_is_cancelled(self):
        watchdog = maintain._IdleWatchdog(0.3)
        with self.assertRaises(grpc.RpcError) as raised:
            self._call(_stream_handler(sends=1, every_s=5), watchdog)
        self.assertTrue(watchdog.fired)
        self.assertEqual(raised.exception.code(), grpc.StatusCode.CANCELLED)
        self.assertTrue(maintain._is_timeout(raised.exception, watchdog))

    def test_a_peer_still_sending_is_not_cut_however_long_it_takes(self):
        # 1.5 s in all, well past the 0.5 s idle limit, but never 0.5 s without a message.
        watchdog = maintain._IdleWatchdog(0.5)
        received = self._call(_stream_handler(sends=10, every_s=0.15), watchdog)
        self.assertEqual(len(received), 10)
        self.assertFalse(watchdog.fired)


if __name__ == "__main__":
    unittest.main()
