"""Routing backends: a ``.onion`` peer is reached through Tor, and only through Tor.

What the tests protect:

* a ``.onion`` name never reaches the local resolver or a direct socket, whatever
  transport the address declared, and whether or not Tor is running;
* an address whose tags mix ``tcp`` with ``tor-onion-v3`` is refused (an old node would
  read it as ``tcp`` and dial the name directly);
* the CONNECT to Tor's HTTPTunnelPort carries the host name, not an IP.
"""
import socket
import threading
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.identity import grpc_transport, transport_stack
    from src.manager import manager
    from src.routing import registry
    from src.routing.base import RoutingUnavailable
    from src.routing.tor import TorBackend, is_onion_host, is_onion_v3
    from src.utils import utils
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

ONION = "a" * 56 + ".onion"
PEER = "peer-1"


class FakeTorProxy:
    """An HTTP CONNECT proxy on loopback that records what it was asked for."""

    def __init__(self, status="HTTP/1.1 200 Connection established"):
        self.status = status
        self.requests = []
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            self.requests.append(data.split(b"\r\n", 1)[0].decode())
            conn.sendall(self.status.encode() + b"\r\n\r\n")
            conn.close()

    def close(self):
        self._listener.close()


def ready_tor(port: int) -> "TorBackend":
    backend = TorBackend()
    backend._http_port = port
    backend._state = "ready"
    return backend


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HostClassificationTests(unittest.TestCase):
    def test_onion_hosts(self):
        self.assertTrue(is_onion_host(ONION))
        self.assertTrue(is_onion_host(ONION.upper() + "."))
        self.assertFalse(is_onion_host("10.0.0.1"))
        self.assertFalse(is_onion_host("example.com"))

    def test_only_a_well_formed_v3_address_is_valid(self):
        self.assertTrue(is_onion_v3(ONION))
        self.assertFalse(is_onion_v3("a" * 16 + ".onion"))  # a v2 name
        self.assertFalse(is_onion_v3("1" * 56 + ".onion"))  # not base32
        self.assertFalse(is_onion_v3("evil.example.com"))

    def test_an_onion_host_goes_to_tor_whatever_its_transport(self):
        self.assertIs(registry.backend_for_host(ONION), registry.TOR)
        self.assertIs(registry.backend_for_transport("tcp", ONION), registry.TOR)
        self.assertIs(registry.backend_for_transport("", ONION), registry.TOR)
        self.assertIs(registry.backend_for_transport("tcp", "10.0.0.1"), registry.CLEARNET)
        self.assertIs(registry.backend_for_transport("tor-onion-v3", ONION), registry.TOR)
        self.assertIsNone(registry.backend_for_transport("udp", "10.0.0.1"))

    def test_mixed_tags_are_refused(self):
        self.assertIs(registry.routed_backend_for_tags(["tor-onion-v3"]), registry.TOR)
        self.assertIsNone(registry.routed_backend_for_tags(["tcp"]))
        for mixed in (["tcp", "tor-onion-v3"], ["tor-onion-v3", "udp"]):
            with self.assertRaises(ValueError):
                registry.routed_backend_for_tags(mixed)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TorSocketTests(unittest.TestCase):
    def setUp(self):
        self.proxy = FakeTorProxy()
        self.addCleanup(self.proxy.close)

    def test_connect_carries_the_host_name_and_never_resolves_it(self):
        backend = ready_tor(self.proxy.port)
        real_getaddrinfo = socket.getaddrinfo

        def no_onion_lookup(host, *args, **kwargs):
            self.assertNotIn("onion", str(host), "the .onion name reached the local resolver")
            return real_getaddrinfo(host, *args, **kwargs)

        with mock.patch.object(socket, "getaddrinfo", side_effect=no_onion_lookup):
            backend.open_socket(ONION, 443, timeout=5).close()

        self.assertEqual(self.proxy.requests, [f"CONNECT {ONION}:443 HTTP/1.1"])

    def test_a_refusal_from_tor_is_a_connection_error(self):
        self.proxy.status = "HTTP/1.1 504 Gateway Timeout"
        with self.assertRaises(ConnectionError):
            ready_tor(self.proxy.port).open_socket(ONION, 443, timeout=5)

    def test_without_tor_nothing_is_dialled(self):
        backend = TorBackend()
        with mock.patch.object(socket, "create_connection") as dial:
            with self.assertRaises(RoutingUnavailable):
                backend.open_socket(ONION, 443, timeout=5)
            with self.assertRaises(RoutingUnavailable):
                backend.grpc_channel_options(ONION)
        dial.assert_not_called()

    def test_the_grpc_channel_uses_the_http_tunnel_port(self):
        backend = ready_tor(self.proxy.port)
        self.assertEqual(
            backend.grpc_channel_options(ONION),
            [("grpc.http_proxy", f"http://127.0.0.1:{self.proxy.port}")],
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChannelTests(unittest.TestCase):
    def test_the_certificate_pre_check_goes_through_the_backend(self):
        proxy = FakeTorProxy()
        self.addCleanup(proxy.close)
        with mock.patch.object(registry, "TOR", ready_tor(proxy.port)), \
             mock.patch.object(registry, "BACKENDS", (registry.CLEARNET, ready_tor(proxy.port))):
            # The proxy hangs up after its status line, so the TLS handshake fails. What
            # matters is that it was asked for the name.
            with self.assertRaises(Exception):
                grpc_transport._fetch_certificate(f"{ONION}:443")
        self.assertEqual(proxy.requests, [f"CONNECT {ONION}:443 HTTP/1.1"])

    def test_an_onion_target_is_never_dialled_directly_when_tor_is_down(self):
        with mock.patch.object(socket, "create_connection") as dial:
            with self.assertRaises(RoutingUnavailable):
                grpc_transport._fetch_certificate(f"{ONION}:443")
        dial.assert_not_called()

    def test_the_channel_to_an_onion_host_carries_the_proxy_option(self):
        tor = ready_tor(4321)
        with mock.patch.object(registry, "BACKENDS", (registry.CLEARNET, tor)), \
             mock.patch.object(grpc_transport, "_fetch_certificate", return_value=b"der"), \
             mock.patch.object(grpc_transport, "peer_id_from_certificate", return_value="id"), \
             mock.patch.object(grpc_transport, "certificate_pem", return_value=b"pem"), \
             mock.patch.object(grpc_transport.grpc, "ssl_channel_credentials", return_value=None), \
             mock.patch.object(grpc_transport.grpc, "secure_channel") as secure_channel:
            grpc_transport.channel_and_peer_id(f"{ONION}:443")
            grpc_transport.channel_and_peer_id("10.0.0.1:443")
        onion_options = secure_channel.call_args_list[0].kwargs["options"]
        clearnet_options = secure_channel.call_args_list[1].kwargs["options"]
        self.assertIn(("grpc.http_proxy", "http://127.0.0.1:4321"), onion_options)
        self.assertFalse([o for o in clearnet_options if o[0] == "grpc.http_proxy"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ProbeAndAddressChoiceTests(unittest.TestCase):
    def setUp(self):
        utils._is_open_cache.clear()
        self.addCleanup(utils._is_open_cache.clear)

    def test_an_onion_address_is_closed_while_tor_is_down(self):
        with mock.patch.object(socket, "create_connection") as dial:
            self.assertFalse(utils.is_open(ONION, 443, "tor-onion-v3"))
            self.assertFalse(utils.is_open(ONION, 443, "tcp"))
        dial.assert_not_called()

    def test_an_onion_address_is_probed_through_tor(self):
        proxy = FakeTorProxy()
        self.addCleanup(proxy.close)
        with mock.patch.object(registry, "BACKENDS", (registry.CLEARNET, ready_tor(proxy.port))):
            self.assertTrue(utils.is_open(ONION, 443, "tor-onion-v3"))
        self.assertEqual(proxy.requests, [f"CONNECT {ONION}:443 HTTP/1.1"])

    def test_udp_is_still_reported_usable_without_a_probe(self):
        self.assertTrue(utils.is_open("10.0.0.1", 53, "udp"))

    def test_only_reachable_addresses_are_yielded(self):
        rows = [
            ("10.0.0.1", 8080, "tcp"),
            (ONION, 443, "tor-onion-v3"),
            ("10.0.0.2", 53, "udp"),
        ]
        with mock.patch.object(utils, "get_peer_directions", return_value=iter(rows)), \
             mock.patch.object(utils, "is_open", side_effect=lambda ip, port, transport: True):
            self.assertEqual(
                list(utils.generate_uris_by_peer_id(PEER)),
                ["10.0.0.1:8080", f"{ONION}:443"],
            )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerIntakeTests(unittest.TestCase):
    def setUp(self):
        self.added = []
        patcher = mock.patch.object(
            manager.sc, "add_peer_uri",
            side_effect=lambda uri, peer_id, transport: self.added.append(
                (uri.ip, uri.port, transport)
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _peer(ip, *tags):
        peer = celaut_pb2.Peer()
        uri = peer.uri.add(ip=ip, port=443)
        uri.transport.tags.extend(tags)
        transport_stack.declare_transport_stack(uri, prose=False)
        return peer

    def test_an_onion_address_is_stored_with_its_own_transport(self):
        stored = manager._store_peer_uris(self._peer(ONION, "tor-onion-v3"), PEER)
        self.assertEqual(stored, [(ONION, 443)])
        self.assertEqual(self.added, [(ONION, 443, "tor-onion-v3")])

    def test_it_is_stored_even_while_tor_is_off_so_enabling_it_later_works(self):
        self.assertFalse(registry.TOR.available())
        self.assertEqual(
            manager._store_peer_uris(self._peer(ONION, "tor-onion-v3"), PEER), [(ONION, 443)]
        )

    def test_mixed_tags_are_refused(self):
        self.assertEqual(manager._store_peer_uris(self._peer(ONION, "tcp", "tor-onion-v3"), PEER), [])
        self.assertEqual(self.added, [])

    def test_an_onion_host_declared_as_tcp_is_refused(self):
        self.assertEqual(manager._store_peer_uris(self._peer(ONION, "tcp"), PEER), [])
        self.assertEqual(self.added, [])

    def test_the_onion_tag_on_an_ip_is_refused(self):
        self.assertEqual(manager._store_peer_uris(self._peer("10.0.0.1", "tor-onion-v3"), PEER), [])

    def test_a_malformed_onion_name_is_refused(self):
        self.assertEqual(manager._store_peer_uris(self._peer("short.onion", "tor-onion-v3"), PEER), [])

    def test_clearnet_addresses_are_unchanged(self):
        self.assertEqual(manager._store_peer_uris(self._peer("10.0.0.1", "tcp"), PEER), [("10.0.0.1", 443)])
        self.assertEqual(self.added, [("10.0.0.1", 443, "tcp")])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DescriptorTests(unittest.TestCase):
    def test_the_onion_transport_never_carries_tcp(self):
        tags, _, formal = transport_stack.transport_component("tor-onion-v3")
        self.assertEqual(tags, ("tor-onion-v3",))
        self.assertIn(b"kind=stream", formal)
        self.assertIn(b"spec=rend-spec-v3", formal)

    def test_tcp_is_the_default_and_unknown_tags_are_refused(self):
        self.assertEqual(transport_stack.transport_component()[0], ("tcp",))
        with self.assertRaises(ValueError):
            transport_stack.transport_component("i2p")

    def test_declare_transport_writes_the_tag(self):
        uri = celaut_pb2.Peer.Uri()
        transport_stack.declare_transport(uri, tag="tor-onion-v3")
        self.assertEqual(list(uri.transport.tags), ["tor-onion-v3"])


if __name__ == "__main__":
    unittest.main()
