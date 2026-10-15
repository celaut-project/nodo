"""The onion service of the gateway, and hidden mode.

What the tests protect:

* the onion key comes from the mnemonic, is unrelated to the identity key, and is
  written in the format tor reads;
* a hidden node puts no IP in its announcement or its Ergo register, and never dials a
  clearnet address directly;
* a call that comes through the onion server is never taken for a local instance.
"""
import base64
import hashlib
import json
import os
import stat
import tempfile
import unittest
from concurrent import futures
from pathlib import Path
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    import grpc
    from protos import celaut_pb2
    from src.gateway import onion_server, utils as gateway_utils
    from src.identity import node_identity, transport_stack
    from src.reputation_system.contracts.ergo import transaction
    from src.routing import onion_key, policy, registry
    from src.routing.base import RoutingUnavailable
    from src.tunneling import delegated_endpoints
    from src.utils import utils
    from src.utils.config import ConfigManager
    from src.utils.config_validation import ConfigValidationError, validate_routing_config
    from src.utils.cost_functions import architecture_resources
    from src.utils.network import uri_expiry
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon about"
)
OTHER_MNEMONIC = "legal winner thank year wave sausage worth useful legal winner thank yellow"
ONION = "a" * 56 + ".onion"
LAN_IP = "93.184.216.34"


def override(**values):
    """Replace ``ConfigManager.get`` for the keys given (dots written as ``__``)."""
    wanted = {key.replace("__", "."): value for key, value in values.items()}
    original = ConfigManager.get

    def get(self, key, default=None):
        return wanted[key] if key in wanted else original(self, key, default)

    return mock.patch.object(ConfigManager, "get", get)


# --- a small ed25519, only to check the key file independently of `cryptography` -------

_P = 2 ** 255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_BASE = (
    15112221349535400772501151409588531511454012693041857206046113283949847762202,
    46316835694926478169428394003475163141307993866256225615783033603165251855960,
    1, 15112221349535400772501151409588531511454012693041857206046113283949847762202
    * 46316835694926478169428394003475163141307993866256225615783033603165251855960 % _P,
)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _public_from_scalar(scalar: int) -> bytes:
    result, addend = (0, 1, 1, 0), _BASE
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    zinv = pow(result[2], _P - 2, _P)
    x, y = result[0] * zinv % _P, result[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OnionKeyTests(unittest.TestCase):
    def test_the_name_is_fixed_by_the_mnemonic(self):
        host = onion_key.host_from_mnemonic(MNEMONIC)
        self.assertEqual(host, onion_key.host_from_mnemonic(MNEMONIC))
        self.assertNotEqual(host, onion_key.host_from_mnemonic(OTHER_MNEMONIC))
        self.assertTrue(registry.TOR.valid_host(host))

    def test_it_is_not_the_identity_key(self):
        seed = onion_key.derive_seed(MNEMONIC)
        identity_public, _ = node_identity._cached_keypair(MNEMONIC)
        self.assertNotEqual(onion_key.public_key(seed).hex(), identity_public)

    def test_a_bad_mnemonic_is_refused(self):
        with self.assertRaises(ValueError):
            onion_key.derive_seed("not a mnemonic")

    def test_the_checksum_matches_a_real_onion_address(self):
        real = "duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad"
        public = base64.b32decode(real.upper())[:32]
        self.assertEqual(onion_key.onion_host(public), real + ".onion")

    def test_the_stored_secret_key_gives_the_stored_public_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, directory = onion_key.write_hidden_service_dir(
                Path(tmp) / "gateway", onion_key.derive_seed(MNEMONIC)
            )
            secret = (directory / "hs_ed25519_secret_key").read_bytes()
            public = (directory / "hs_ed25519_public_key").read_bytes()
            self.assertEqual(secret[:32], b"== ed25519v1-secret: type0 ==\x00\x00\x00")
            self.assertEqual(public[:32], b"== ed25519v1-public: type0 ==\x00\x00\x00")
            self.assertEqual(len(secret), 32 + 64)
            scalar = int.from_bytes(secret[32:64], "little")
            # What tor does with the file: public key = scalar * base point.
            self.assertEqual(_public_from_scalar(scalar), public[32:])
            self.assertEqual(onion_key.onion_host(public[32:]), host)
            self.assertEqual((directory / "hostname").read_text(), host + "\n")

    def test_tor_needs_the_directory_and_files_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, directory = onion_key.write_hidden_service_dir(
                Path(tmp) / "gateway", onion_key.derive_seed(MNEMONIC)
            )
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for name in ("hs_ed25519_secret_key", "hs_ed25519_public_key", "hostname"):
                self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PolicyTests(unittest.TestCase):
    def test_announce_follows_enabled_unless_set(self):
        cases = [
            # (ENABLED, ANNOUNCE, HIDDEN, expected)
            ("auto", "auto", False, False),
            (True, "auto", False, True),
            (False, "auto", False, False),
            ("auto", "auto", True, True),
            ("auto", True, False, True),
            (True, False, False, False),
        ]
        for enabled, announce, hidden, expected in cases:
            with override(routing__tor__ENABLED=enabled, routing__tor__ANNOUNCE=announce,
                          routing__HIDDEN=hidden):
                self.assertEqual(policy.announce_onion(), expected, (enabled, announce, hidden))

    def test_hidden_must_be_a_real_boolean(self):
        with override(routing__HIDDEN="yes"):
            self.assertFalse(policy.hidden_mode())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HiddenRoutingTests(unittest.TestCase):
    def test_a_clearnet_host_is_refused_in_hidden_mode(self):
        with override(routing__HIDDEN=True):
            self.assertIs(registry.backend_for_host("10.0.0.1"), registry.BLOCKED)
            with self.assertRaises(RoutingUnavailable):
                registry.BLOCKED.open_socket("10.0.0.1", 80, 1)
            with self.assertRaises(RoutingUnavailable):
                registry.BLOCKED.grpc_channel_options("10.0.0.1")
            self.assertFalse(registry.usable("tcp", "10.0.0.1"))
            self.assertIs(registry.backend_for_host(ONION), registry.TOR)

    def test_through_an_exit_when_the_operator_allows_it(self):
        with override(routing__HIDDEN=True, routing__tor__CLEARNET_VIA_EXIT=True):
            self.assertIs(registry.backend_for_host("10.0.0.1"), registry.TOR)

    def test_a_node_that_is_not_hidden_dials_the_clearnet_directly(self):
        with override(routing__HIDDEN=False):
            self.assertIs(registry.backend_for_host("10.0.0.1"), registry.CLEARNET)

    def test_udp_is_still_nobodys_to_dial_in_hidden_mode(self):
        with override(routing__HIDDEN=True):
            self.assertIsNone(registry.backend_for_transport("udp", "10.0.0.1"))
            self.assertFalse(utils._is_stream_transport("10.0.0.1", "udp"))
            self.assertTrue(utils._is_stream_transport("10.0.0.1", "tcp"))

    def test_the_probe_of_a_clearnet_peer_never_touches_the_network(self):
        utils._is_open_cache.clear()
        self.addCleanup(utils._is_open_cache.clear)
        with override(routing__HIDDEN=True), \
                mock.patch("socket.socket") as sock, \
                mock.patch("socket.create_connection") as dial:
            self.assertFalse(utils.is_open("10.0.0.1", 8080, "tcp"))
        sock.assert_not_called()
        dial.assert_not_called()

    def test_hidden_mode_always_uses_the_tunnel_for_instances(self):
        with override(routing__HIDDEN=True, network__DELEGATION_TUNNEL_POLICY="never"):
            self.assertEqual(delegated_endpoints._policy(), delegated_endpoints.POLICY_ALWAYS)
        with override(routing__HIDDEN=False, network__DELEGATION_TUNNEL_POLICY="never"):
            self.assertEqual(delegated_endpoints._policy(), delegated_endpoints.POLICY_NEVER)

    def test_no_expiry_in_hidden_mode(self):
        with override(routing__HIDDEN=True, network__ADDRESS_VALIDITY_SECONDS=3600):
            self.assertEqual(uri_expiry(1000), 0)
        with override(routing__HIDDEN=False, network__ADDRESS_VALIDITY_SECONDS=3600):
            self.assertEqual(uri_expiry(1000), 4600)

    def test_benchmark_scores_are_rounded_in_hidden_mode(self):
        self.assertEqual(architecture_resources._two_significant_digits(7), 7)
        self.assertEqual(architecture_resources._two_significant_digits(99), 99)
        self.assertEqual(architecture_resources._two_significant_digits(1234), 1200)
        self.assertEqual(architecture_resources._two_significant_digits(98765), 99000)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.host = registry.TOR.configure_onion(MNEMONIC, 4000, 8090)
        self.addCleanup(setattr, registry.TOR, "_onion", None)
        # The announcement also reads payment contracts and proofs from the database.
        for target, value in (
            (gateway_utils, "_local_payment_contracts"),
            ("src.reputation_system.fetch.local_proofs", None),
        ):
            patcher = (
                mock.patch.object(target, value, return_value=[])
                if value else mock.patch(target, return_value=[])
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        registry.TOR._http_port, registry.TOR._state = 1, "ready"
        self.addCleanup(setattr, registry.TOR, "_state", "stopped")
        self.addCleanup(setattr, registry.TOR, "_http_port", None)

    def _uris(self, **config):
        with override(**config), \
                mock.patch.object(gateway_utils, "_public_host", return_value=LAN_IP), \
                mock.patch.object(gateway_utils, "_gateway_port", return_value=8090), \
                mock.patch.object(gateway_utils, "_public_port", return_value=8090), \
                mock.patch.object(gateway_utils.ni, "interfaces", return_value=[]):
            return gateway_utils._uris_for_all_interfaces()

    def test_hidden_announces_only_the_onion_address(self):
        uris = self._uris(routing__HIDDEN=True)
        self.assertEqual([(u.ip, u.port) for u in uris], [(self.host, 8090)])

    def test_hidden_announces_nothing_until_tor_is_up(self):
        registry.TOR._state = "starting"
        self.assertEqual(self._uris(routing__HIDDEN=True), [])

    def test_a_node_that_is_not_hidden_announces_both(self):
        uris = self._uris(routing__HIDDEN=False, routing__tor__ENABLED=True)
        self.assertEqual([u.ip for u in uris], [LAN_IP, self.host])

    def test_without_announce_there_is_no_onion_address(self):
        uris = self._uris(routing__HIDDEN=False, routing__tor__ENABLED="auto")
        self.assertEqual([u.ip for u in uris], [LAN_IP])

    def test_each_address_declares_its_own_transport(self):
        uris = self._uris(routing__HIDDEN=False, routing__tor__ENABLED=True)
        with override(routing__HIDDEN=False):
            peer = gateway_utils._unsigned_peer(uris, quiet=True)
        self.assertEqual(
            [(u.ip, list(u.transport.tags)) for u in peer.uri],
            [(LAN_IP, ["tcp"]), (self.host, ["tor-onion-v3"])],
        )

    def test_a_signed_hidden_announcement_has_no_ip_and_no_expiry(self):
        with override(routing__HIDDEN=True, network__ADDRESS_VALIDITY_SECONDS=3600):
            peer = gateway_utils._build_peer(self._uris(routing__HIDDEN=True))
        self.assertEqual([u.ip for u in peer.uri], [self.host])
        self.assertEqual([u.expiry_unix_timestamp for u in peer.uri], [0])
        self.assertNotIn(LAN_IP.encode(), peer.SerializeToString())
        self.assertTrue(peer.signature)

    def test_the_announced_address_is_one_a_peer_accepts(self):
        from src.manager import manager
        uris = self._uris(routing__HIDDEN=True)
        peer = gateway_utils._unsigned_peer(uris, quiet=True)
        added = []
        with mock.patch.object(manager.sc, "add_peer_uri",
                               side_effect=lambda uri, peer_id, transport: added.append(transport)):
            stored = manager._store_peer_uris(peer, "peer-1")
        self.assertEqual(stored, [(self.host, 8090)])
        self.assertEqual(added, ["tor-onion-v3"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LedgerRegisterTests(unittest.TestCase):
    def _data(self, **config):
        values = dict(
            SUBMIT_NETWORK_ADDRESS_TO_REPUTATION_PROOF=True,
            network__PUBLIC_IP=LAN_IP,
            network__PUBLIC_TCP_PORT="",
            identity__MNEMONIC=MNEMONIC,
            routing__tor__ANNOUNCE="auto",
        )
        values.update(config)
        with override(**values), \
                mock.patch.object(ConfigManager, "get_gateway_port", return_value=8090), \
                mock.patch("src.utils.network.get_local_ip", return_value=LAN_IP):
            return transaction._self_network_data()

    def test_a_hidden_node_publishes_the_onion_address_and_no_ip(self):
        data = self._data(routing__HIDDEN=True)
        self.assertNotIn(LAN_IP, data)
        self.assertIn(onion_key.host_from_mnemonic(MNEMONIC), data)
        self.assertIn("tor-onion-v3", data)

    def test_a_node_that_is_not_hidden_and_does_not_announce_is_unchanged(self):
        data = self._data(routing__HIDDEN=False, routing__tor__ENABLED="auto")
        self.assertIn(LAN_IP, data)
        self.assertNotIn(".onion", data)

    def test_a_node_that_announces_publishes_both(self):
        data = self._data(routing__HIDDEN=False, routing__tor__ENABLED=True)
        self.assertIn(LAN_IP, data)
        self.assertIn(onion_key.host_from_mnemonic(MNEMONIC), data)

    def test_the_published_register_is_a_peer_a_reader_can_parse(self):
        from google.protobuf import json_format
        data = self._data(routing__HIDDEN=True)
        peer = json_format.Parse(data, celaut_pb2.Peer())
        self.assertEqual([list(u.transport.tags) for u in peer.uri], [["tor-onion-v3"]])


class _Echo(grpc.GenericRpcHandler if IMPORT_ERROR is None else object):
    """A gateway stand-in: each method answers with what the context says the caller is."""

    def service(self, details):
        method = details.method.rsplit("/", 1)[-1]

        def peer(context):
            return context.peer().encode()

        if method == "UnaryUnary":
            return grpc.unary_unary_rpc_method_handler(lambda request, context: peer(context))
        if method == "StreamStream":
            return grpc.stream_stream_rpc_method_handler(
                lambda requests, context: iter([peer(context) for _ in requests])
            )
        if method == "UnaryStream":
            return grpc.unary_stream_rpc_method_handler(
                lambda request, context: iter([peer(context)])
            )
        if method == "StreamUnary":
            return grpc.stream_unary_rpc_method_handler(
                lambda requests, context: [r for r in requests] and peer(context)
            )
        return None


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OnionServerTests(unittest.TestCase):
    def _server(self, interceptors):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2), interceptors=interceptors)
        server.add_generic_rpc_handlers((_Echo(),))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, 0)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        return channel

    def _calls(self, channel):
        return {
            "uu": channel.unary_unary("/t/UnaryUnary")(b"x"),
            "us": list(channel.unary_stream("/t/UnaryStream")(b"x")),
            "su": channel.stream_unary("/t/StreamUnary")(iter([b"x"])),
            "ss": list(channel.stream_stream("/t/StreamStream")(iter([b"x"]))),
        }

    def test_every_kind_of_call_is_marked_as_coming_from_onion(self):
        replies = self._calls(self._server([onion_server.OnionOriginInterceptor()]))
        for kind, reply in replies.items():
            reply = reply[0] if isinstance(reply, list) else reply
            self.assertTrue(reply.startswith(b"onion:ipv4:127.0.0.1:"), (kind, reply))

    def test_the_ordinary_server_is_not_touched(self):
        replies = self._calls(self._server([]))
        for kind, reply in replies.items():
            reply = reply[0] if isinstance(reply, list) else reply
            self.assertTrue(reply.startswith(b"ipv4:127.0.0.1:"), (kind, reply))

    def test_an_onion_caller_has_no_ip_so_it_cannot_be_a_local_instance(self):
        peer = onion_server.OnionContext(mock.Mock(peer=lambda: "ipv4:127.0.0.1:5555")).peer()
        self.assertIsNone(utils.get_only_the_ip_from_context(context_peer=peer))
        self.assertEqual(utils.get_only_the_ip_from_context(context_peer="ipv4:127.0.0.1:5555"), "127.0.0.1")

    def test_the_wrapper_passes_everything_else_to_the_real_context(self):
        real = mock.Mock()
        real.peer.return_value = "ipv4:127.0.0.1:1"
        real.is_active.return_value = True
        wrapped = onion_server.OnionContext(real)
        self.assertTrue(wrapped.is_active())
        wrapped.set_code(grpc.StatusCode.NOT_FOUND)
        real.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)

    def test_an_onion_caller_needs_a_client_id(self):
        from src.gateway import client_gate
        context = onion_server.OnionContext(mock.Mock(peer=lambda: "ipv4:127.0.0.1:5555"))
        with mock.patch.object(client_gate, "get_internal_service_id_by_uri",
                               side_effect=lambda uri: "local-instance" if uri == "127.0.0.1" else None), \
                mock.patch.object(client_gate.sc, "client_exists", return_value=False):
            with self.assertRaises(client_gate.ClientRequired):
                client_gate.require_caller(context, "")
            # ...and a plain connection from the same address is the local instance.
            plain = mock.Mock(peer=lambda: "ipv4:127.0.0.1:5555")
            self.assertEqual(client_gate.require_caller(plain, ""), "local-instance")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OnionCallerRpcTests(unittest.TestCase):
    """The RPCs that look for a local instance by the IP of the caller."""

    def test_an_onion_caller_cannot_modify_the_resources_of_an_instance(self):
        from src.gateway import gateway
        context = onion_server.OnionContext(mock.Mock(peer=lambda: "ipv4:127.0.0.1:5555"))
        with mock.patch.object(gateway, "get_internal_service_id_by_uri",
                               side_effect=lambda uri: "local-instance" if uri == "127.0.0.1" else None), \
                mock.patch.object(gateway, "spend_mu") as spend:
            with self.assertRaises(Exception) as raised:
                list(gateway.Gateway.ModifyServiceSystemResources(mock.Mock(), iter(()), context))
        self.assertIn("has no IP address", str(raised.exception))
        spend.assert_not_called()

    def test_no_instance_is_looked_up_without_an_address(self):
        from src.database.sql_connection import SQLConnection
        connection = mock.Mock()
        for uri in (None, ""):
            self.assertIsNone(SQLConnection.get_local_instance_id_by_uri(connection, uri))
        connection._execute.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServeOnionTests(unittest.TestCase):
    def setUp(self):
        from src import serve
        self.serve = serve
        self.addCleanup(setattr, registry.TOR, "_onion", None)

    def test_nothing_is_started_when_the_node_does_not_announce(self):
        with override(routing__HIDDEN=False, routing__tor__ENABLED="auto"):
            self.assertIsNone(self.serve._start_onion_server(8090))
        self.assertIsNone(registry.TOR.onion_host())

    def test_the_onion_server_listens_on_loopback_with_the_node_certificate(self):
        from src.identity import grpc_transport
        with override(routing__HIDDEN=False, routing__tor__ENABLED=True,
                      identity__MNEMONIC=MNEMONIC):
            server = self.serve._start_onion_server(8090)
        self.assertIsNotNone(server)
        self.addCleanup(server.stop, 0)
        host, _, local_port, virtual_port = registry.TOR._onion
        self.assertEqual(host, onion_key.host_from_mnemonic(MNEMONIC))
        self.assertEqual(virtual_port, 8090)
        channel, peer_id = grpc_transport.channel_and_peer_id(f"127.0.0.1:{local_port}")
        channel.close()
        # The certificate is the node's own; it is cached for the process, so which
        # mnemonic it was made from depends on what ran before this test.
        self.assertRegex(peer_id, r"^[0-9a-f]{64}$")

    def test_a_hidden_node_stops_if_it_cannot_host_its_onion_service(self):
        from src.gateway import onion_server as module
        with override(routing__HIDDEN=True, identity__MNEMONIC=MNEMONIC), \
                mock.patch.object(module, "bind_onion_server", return_value=0):
            with self.assertRaises(SystemExit):
                self.serve._start_onion_server(8090)

    def test_a_node_that_is_not_hidden_carries_on_without_it(self):
        from src.gateway import onion_server as module
        with override(routing__HIDDEN=False, routing__tor__ENABLED=True,
                      identity__MNEMONIC=MNEMONIC), \
                mock.patch.object(module, "bind_onion_server", return_value=0):
            self.assertIsNone(self.serve._start_onion_server(8090))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HiddenConfigTests(unittest.TestCase):
    def test_a_valid_hidden_config(self):
        validate_routing_config({"routing": {"HIDDEN": True}})
        validate_routing_config({"routing": {"HIDDEN": True, "tor": {"ENABLED": True, "ANNOUNCE": True}}})
        validate_routing_config({"routing": {"HIDDEN": True}, "network": {"PUBLIC_IP": ""}})

    def test_a_hidden_node_cannot_be_given_what_would_reveal_it(self):
        for bad in (
            {"routing": {"HIDDEN": True, "tor": {"ENABLED": False}}},
            {"routing": {"HIDDEN": True, "tor": {"ANNOUNCE": False}}},
            {"routing": {"HIDDEN": True}, "network": {"PUBLIC_IP": "93.184.216.34"}},
            {"routing": {"HIDDEN": True}, "network": {"ANNOUNCE_PRIVATE_ADDRESSES": True}},
            {"routing": {"HIDDEN": "yes"}},
            {"routing": {"tor": {"ANNOUNCE": "maybe"}}},
            {"routing": {"tor": {"CLEARNET_VIA_EXIT": "yes"}}},
        ):
            with self.assertRaises(ConfigValidationError, msg=bad):
                validate_routing_config(bad)

    def test_the_same_values_are_fine_when_the_node_is_not_hidden(self):
        validate_routing_config(
            {"routing": {"HIDDEN": False, "tor": {"ENABLED": False, "ANNOUNCE": False}},
             "network": {"PUBLIC_IP": "93.184.216.34"}}
        )


if __name__ == "__main__":
    unittest.main()
