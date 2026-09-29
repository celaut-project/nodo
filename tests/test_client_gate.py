"""client_gate: the client_id gate every RPC but GenerateClient now goes through
(issue #428).

Two properties matter: a local instance of this node is exempt (identified the way
ModifyServiceSystemResources already identifies one), and everyone else needs a
client_id this node already minted, which is then bounded by a shared per-client_id
sliding window -- too many calls too fast and the client_id is quarantined, refused
outright until the quarantine expires.
"""
import unittest
from unittest.mock import patch
from uuid import uuid4

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from bee_rpc import client as bee
    from protos import celaut_pb2
    from src.gateway import client_gate
    from src.gateway.client_gate import (
        ClientRequired,
        _ClientCallWindow,
        parse_with_client,
        require_caller,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


class _Context:
    """A stand-in for grpc's ServicerContext: only `.peer()` is ever read here."""

    def __init__(self, ip: str = "9.9.9.9", port: int = 4442):
        self._peer = f"ipv4:{ip}:{port}"

    def peer(self) -> str:
        return self._peer


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RequireCallerTests(unittest.TestCase):
    def setUp(self):
        # Fresh window per test: a quarantine or a count left over from another test
        # must not leak into this one.
        self._orig_window = client_gate._window
        client_gate._window = _ClientCallWindow()
        self.addCleanup(setattr, client_gate, "_window", self._orig_window)

        self.known_clients = set()
        self.existence_patcher = patch.object(
            client_gate.sc, "client_exists",
            side_effect=lambda client_id: client_id in self.known_clients,
        )
        self.existence_patcher.start()
        self.addCleanup(self.existence_patcher.stop)

        self.local_instance_token = None
        self.local_patcher = patch.object(
            client_gate, "get_internal_service_id_by_uri",
            side_effect=lambda uri: self.local_instance_token,
        )
        self.local_patcher.start()
        self.addCleanup(self.local_patcher.stop)

    def _new_client(self) -> str:
        client_id = uuid4().hex
        self.known_clients.add(client_id)
        return client_id

    def test_a_local_instance_is_exempt_with_no_client_id_at_all(self):
        self.local_instance_token = "instance-token"
        self.assertEqual(require_caller(_Context()), "instance-token")

    def test_a_local_instance_is_exempt_even_with_a_bogus_client_id(self):
        # Its own address is what identifies it; a client_id it happens to also send
        # (or not) is irrelevant.
        self.local_instance_token = "instance-token"
        self.assertEqual(require_caller(_Context(), "not-a-real-client"), "instance-token")

    def test_no_client_id_and_no_local_instance_is_refused(self):
        with self.assertRaises(ClientRequired):
            require_caller(_Context())

    def test_a_client_id_this_node_never_minted_is_refused(self):
        with self.assertRaises(ClientRequired):
            require_caller(_Context(), uuid4().hex)

    def test_a_malformed_client_id_is_refused_without_touching_the_database(self):
        # The shape check is pure string work; nothing here should need a DB read to
        # reject an id that could not possibly have been minted.
        for bad in ("", "not-a-uuid", "x" * 32, uuid4().hex.upper(), uuid4().hex + "0"):
            with self.subTest(bad=bad):
                with self.assertRaises(ClientRequired):
                    require_caller(_Context(), bad)
        self.existence_patcher.stop()
        try:
            with patch.object(
                client_gate.sc, "client_exists",
                side_effect=AssertionError("client_exists should not have been called"),
            ):
                with self.assertRaises(ClientRequired):
                    require_caller(_Context(), "still-not-a-uuid")
        finally:
            self.existence_patcher.start()

    def test_a_known_client_id_is_accepted(self):
        client_id = self._new_client()
        self.assertEqual(require_caller(_Context(), client_id), client_id)

    def test_too_many_calls_in_the_window_are_quarantined(self):
        client_id = self._new_client()
        with patch.object(client_gate, "_max_calls_per_window", return_value=3):
            for _ in range(3):
                require_caller(_Context(), client_id)
            with self.assertRaises(ClientRequired):
                require_caller(_Context(), client_id)

    def test_quarantine_lifts_after_it_expires(self):
        client_id = self._new_client()
        with patch.object(client_gate, "_max_calls_per_window", return_value=1), \
             patch.object(client_gate, "_quarantine_seconds", return_value=100):
            require_caller(_Context(), client_id)
            with self.assertRaises(ClientRequired):
                require_caller(_Context(), client_id)

            with patch("time.monotonic", return_value=__import__("time").monotonic() + 101):
                # Lifted, and this call itself starts a fresh window rather than
                # counting against the one that just expired.
                require_caller(_Context(), client_id)

    def test_calls_outside_the_window_do_not_count_against_the_limit(self):
        # max_calls=2 with a 10s window: two calls back to back is fine either way,
        # but a *third* only stays fine if the first has aged out of the window --
        # the point being tested, since without eviction it would be the third call
        # in the window and would quarantine.
        client_id = self._new_client()
        with patch.object(client_gate, "_max_calls_per_window", return_value=2), \
             patch.object(client_gate, "_window_seconds", return_value=10):
            require_caller(_Context(), client_id)
            require_caller(_Context(), client_id)

            with patch("time.monotonic", return_value=__import__("time").monotonic() + 11):
                require_caller(_Context(), client_id)

    def test_one_clients_flood_does_not_quarantine_another(self):
        flooding, quiet = self._new_client(), self._new_client()
        with patch.object(client_gate, "_max_calls_per_window", return_value=1):
            require_caller(_Context(), flooding)
            with self.assertRaises(ClientRequired):
                require_caller(_Context(), flooding)

            # The other client_id's own budget is untouched.
            self.assertEqual(require_caller(_Context(), quiet), quiet)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TrackedClientsCapTests(unittest.TestCase):
    """Bounded the way utils.is_open's cache is: a flood of distinct client_ids
    (each one still priced by GenerateClient's PoW, issue #361) must not grow this
    window without limit."""

    def test_the_window_evicts_the_oldest_inactive_client_once_the_cap_is_hit(self):
        window = _ClientCallWindow()
        with patch.object(client_gate, "_MAX_TRACKED_CLIENTS", 3), \
             patch.object(client_gate, "_max_calls_per_window", return_value=100):
            first = uuid4().hex
            window.allow(first)
            window.allow(uuid4().hex)
            window.allow(uuid4().hex)
            self.assertIn(first, window._calls)

            window.allow(uuid4().hex)  # a fourth, over the cap of 3
            self.assertNotIn(first, window._calls)
            self.assertEqual(len(window._calls), 3)

    def test_quarantined_clients_are_also_capped_not_kept_forever(self):
        # A client_id quarantined and then abandoned (never asks again) must not sit
        # in _quarantined_until forever -- that dict needs its own cap, the same as
        # _calls, or a flood of throwaway client_ids each quarantined once is an
        # unbounded memory sink by a different name.
        window = _ClientCallWindow()
        with patch.object(client_gate, "_MAX_TRACKED_CLIENTS", 3), \
             patch.object(client_gate, "_max_calls_per_window", return_value=1):
            first = uuid4().hex
            window.allow(first)
            window.allow(first)  # over budget: quarantined
            self.assertIn(first, window._quarantined_until)

            for _ in range(3):  # three more distinct clients, each quarantined once
                cid = uuid4().hex
                window.allow(cid)
                window.allow(cid)

            self.assertNotIn(first, window._quarantined_until)
            self.assertLessEqual(len(window._quarantined_until), 3)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ParseWithClientWireTests(unittest.TestCase):
    """The envelope IntroducePeer (and the rest) now share: a payload message and an
    optional Client, in whichever order the caller sent them."""

    @staticmethod
    def _round_trip(messages, indices):
        buffers = list(bee.serialize_to_buffer(message_iterator=messages, indices=dict(indices)))
        return list(iter(buffers))

    def test_a_payload_with_no_client_parses_with_an_empty_client_id(self):
        peer = celaut_pb2.Peer(public_key="abc")
        buffers = self._round_trip([peer], {1: celaut_pb2.Peer, 2: celaut_pb2.Client})
        payload, client_id = parse_with_client(iter(buffers), payload_type=celaut_pb2.Peer)
        self.assertEqual(payload, peer)
        self.assertEqual(client_id, "")

    def test_a_client_alongside_the_payload_is_extracted_regardless_of_order(self):
        peer = celaut_pb2.Peer(public_key="abc")
        client = celaut_pb2.Client(client_id=uuid4().hex)

        for messages in ([peer, client], [client, peer]):
            with self.subTest(order=[type(m).__name__ for m in messages]):
                buffers = self._round_trip(
                    messages, {1: celaut_pb2.Peer, 2: celaut_pb2.Client}
                )
                payload, client_id = parse_with_client(iter(buffers), payload_type=celaut_pb2.Peer)
                self.assertEqual(payload, peer)
                self.assertEqual(client_id, client.client_id)

    def test_a_caller_that_sends_nothing_parses_to_no_payload_and_no_client(self):
        from bee_rpc import buffer_pb2 as bee_buffer_pb2
        buffers = list(bee.serialize_to_buffer(message_iterator=bee_buffer_pb2.Empty(), indices={}))
        payload, client_id = parse_with_client(iter(buffers), payload_type=celaut_pb2.Peer)
        self.assertIsNone(payload)
        self.assertEqual(client_id, "")


GATEWAY_IMPORT_ERROR = None
try:
    from src.gateway import gateway as gateway_module
    from src.gateway.gateway import Gateway
except Exception as import_exc:  # pragma: no cover - environment-dependent
    GATEWAY_IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None or GATEWAY_IMPORT_ERROR is not None,
                 f"Missing runtime dependencies: {IMPORT_ERROR or GATEWAY_IMPORT_ERROR}")
class IntroducePeerGateTests(unittest.TestCase):
    """IntroducePeer itself (issue #428's headline case): the gate runs, and runs
    before add_peer_instance's DB write -- not after."""

    def setUp(self):
        self._orig_window = client_gate._window
        client_gate._window = _ClientCallWindow()
        self.addCleanup(setattr, client_gate, "_window", self._orig_window)

        self.known_clients = set()
        self.existence_patcher = patch.object(
            client_gate.sc, "client_exists",
            side_effect=lambda client_id: client_id in self.known_clients,
        )
        self.existence_patcher.start()
        self.addCleanup(self.existence_patcher.stop)

        self.local_patcher = patch.object(
            client_gate, "get_internal_service_id_by_uri", return_value=None,
        )
        self.local_patcher.start()
        self.addCleanup(self.local_patcher.stop)

        self.add_peer_calls = []
        self.add_peer_patcher = patch.object(
            gateway_module, "add_peer_instance",
            side_effect=lambda peer: self.add_peer_calls.append(peer) or "a-peer-id",
        )
        self.add_peer_patcher.start()
        self.addCleanup(self.add_peer_patcher.stop)

    @staticmethod
    def _request(messages):
        return iter(list(bee.serialize_to_buffer(
            message_iterator=messages,
            indices={1: celaut_pb2.Peer, 2: celaut_pb2.Client},
        )))

    def test_without_a_client_id_the_peer_is_never_even_looked_at(self):
        peer = celaut_pb2.Peer(public_key="abc")
        with self.assertRaises(ClientRequired):
            list(Gateway().IntroducePeer(self._request([peer]), _Context()))
        self.assertEqual(self.add_peer_calls, [])

    def test_an_unrecognized_client_id_is_refused_before_the_db_write(self):
        peer = celaut_pb2.Peer(public_key="abc")
        client = celaut_pb2.Client(client_id=uuid4().hex)
        with self.assertRaises(ClientRequired):
            list(Gateway().IntroducePeer(self._request([peer, client]), _Context()))
        self.assertEqual(self.add_peer_calls, [])

    def test_a_known_client_id_lets_the_announcement_through(self):
        peer = celaut_pb2.Peer(public_key="abc")
        client_id = uuid4().hex
        self.known_clients.add(client_id)

        list(Gateway().IntroducePeer(
            self._request([peer, celaut_pb2.Client(client_id=client_id)]), _Context()
        ))

        self.assertEqual(self.add_peer_calls, [peer])


@unittest.skipIf(IMPORT_ERROR is not None or GATEWAY_IMPORT_ERROR is not None,
                 f"Missing runtime dependencies: {IMPORT_ERROR or GATEWAY_IMPORT_ERROR}")
class AssociateClientGateTests(unittest.TestCase):
    """AssociateClient (issue #428's deferred binding half): gated by the client_id
    it is itself associating -- no separate envelope needed -- and it delegates the
    actual binding decision to associate_client_with_peer verbatim."""

    def setUp(self):
        self._orig_window = client_gate._window
        client_gate._window = _ClientCallWindow()
        self.addCleanup(setattr, client_gate, "_window", self._orig_window)

        self.known_clients = set()
        self.existence_patcher = patch.object(
            client_gate.sc, "client_exists",
            side_effect=lambda client_id: client_id in self.known_clients,
        )
        self.existence_patcher.start()
        self.addCleanup(self.existence_patcher.stop)

        self.local_patcher = patch.object(
            client_gate, "get_internal_service_id_by_uri", return_value=None,
        )
        self.local_patcher.start()
        self.addCleanup(self.local_patcher.stop)

        self.associate_calls = []
        self.associate_patcher = patch.object(
            gateway_module, "associate_client_with_peer",
            side_effect=lambda client_id, peer_id, signature: (
                self.associate_calls.append((client_id, peer_id, signature)) or (True, "")
            ),
        )
        self.associate_patcher.start()
        self.addCleanup(self.associate_patcher.stop)

    @staticmethod
    def _request(client):
        return iter(list(bee.serialize_to_buffer(message_iterator=client, indices=celaut_pb2.Client)))

    def test_an_unrecognized_client_id_is_refused_before_associating_anything(self):
        client = celaut_pb2.Client(client_id=uuid4().hex, peer_id="some-peer", signature="sig")
        with self.assertRaises(ClientRequired):
            list(Gateway().AssociateClient(self._request(client), _Context()))
        self.assertEqual(self.associate_calls, [])

    def test_a_known_client_id_is_associated_and_the_answer_is_returned(self):
        client_id = uuid4().hex
        self.known_clients.add(client_id)
        client = celaut_pb2.Client(client_id=client_id, peer_id="some-peer", signature="sig")

        responses = list(Gateway().AssociateClient(self._request(client), _Context()))
        result = next(bee.parse_from_buffer(
            request_iterator=iter(responses),
            indices=celaut_pb2.AssociateClientOutput,
            partitions_message_mode=True,
        ))

        self.assertEqual(self.associate_calls, [(client_id, "some-peer", "sig")])
        self.assertTrue(result.bound)


if __name__ == "__main__":
    unittest.main()
