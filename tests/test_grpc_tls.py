"""The node's TLS identity, and the two ports the gateway serves (issue #257).

What these pin, in order of what would hurt most if it broke:

* the certificate is worthless unless its extension is signed over *its own* public
  key -- otherwise the extension could be lifted from a legitimate node's certificate
  and stapled onto an attacker's, which is the whole attack the binding prevents;
* an address that answers with a certificate for a different identity is refused, which
  is the check that did not exist while every channel was plaintext;
* the server the node actually serves with, and the channels the node actually dials
  with, talk to each other end to end -- the spike in the issue was a deduction, and
  the parts it deduced (a stdlib pre-flight against a gRPC server, a `CA:TRUE`
  self-signed certificate accepted as its own trust anchor) are exercised here;
* the plaintext port stays plaintext, and is the one a service is handed -- peers and
  the CLI get TLS with no exception, but a service we execute speaks plain gRPC over a
  hop that never leaves the host;
* that plaintext port binds one address and not every interface -- it serves the same
  unauthenticated `Gateway`, so a wildcard bind would hand the whole API to any host
  that can route here, which is what the TLS port exists to prevent.
"""
import datetime
import ssl
import unittest
import unittest.mock
import uuid
from concurrent import futures

IMPORT_ERROR = None
try:
    import grpc
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.gateway import utils as gateway_utils
    from src.utils.node_identity import (
    get_node_public_key_hex,
    normalize_public_key_hex,
)
    from src.utils import grpc_transport, tls_identity
    from src.utils.tls_identity import (
        HOST_KEY_EXTENSION_OID,
        TLS_SERVER_NAME,
        CertificateError,
        certificate_and_key,
        peer_id_from_certificate,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def _serve(credentials=None, plaintext_too=False):
    """A one-method gRPC server. Returns (server, target[, plaintext_target])."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler("Echo", {
            "Echo": grpc.unary_unary_rpc_method_handler(
                lambda request, context: request,
                request_deserializer=bytes,
                response_serializer=bytes,
            )
        }),
    ))
    if credentials is None:
        port = server.add_insecure_port("127.0.0.1:0")
    else:
        port = server.add_secure_port("127.0.0.1:0", credentials)
    plaintext_port = server.add_insecure_port("127.0.0.1:0") if plaintext_too else None
    server.start()
    if plaintext_too:
        return server, f"127.0.0.1:{port}", f"127.0.0.1:{plaintext_port}"
    return server, f"127.0.0.1:{port}"


def _echo(channel, payload=b"ping", timeout=10):
    call = channel.unary_unary(
        "/Echo/Echo", request_serializer=bytes, response_deserializer=bytes
    )
    return call(payload, timeout=timeout)


def _certificate_der():
    return x509.load_pem_x509_certificate(certificate_and_key()[0]).public_bytes(
        serialization.Encoding.DER
    )


def _certificate_with_extensions(extensions):
    """A self-signed P-256 certificate holding a fresh key and the given extensions."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, TLS_SERVER_NAME)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2000, 1, 1))
        .not_valid_after(datetime.datetime(9999, 12, 31))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    )
    for extension in extensions:
        builder = builder.add_extension(extension, critical=False)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ProtocolConstantsTests(unittest.TestCase):
    """What the `["tls", "grpc"]` tag pair actually commits a node to.

    These two values are agreed between peers, not chosen per node: one is how the
    extension is found, the other is what the signature is computed over. A node using
    different ones speaks a different transport and has to declare other tags on its
    `Peer.Uri.protocol_stack`, so changing either here silently would make this node
    announce a stack it does not speak.
    """

    def test_the_host_key_oid_is_the_documented_uuid(self):
        # Derived from the project name alone, under the ITU-T X.667 arc `2.25`, so it
        # can be recomputed rather than taken on faith.
        self.assertEqual(
            HOST_KEY_EXTENSION_OID.dotted_string,
            f"2.25.{uuid.uuid5(uuid.NAMESPACE_OID, 'CELAUT').int}",
        )

    def test_the_signed_payload_cannot_be_confused_with_a_peer_payload(self):
        # Both are signed by the identity key, so the prefix is what keeps a signature
        # over one from verifying as a signature over the other. A canonical peer
        # payload opens with a lowercase public key hex, which can never start this way.
        payload = tls_identity._host_key_payload(b"\x01\x02\x03")
        self.assertTrue(payload.startswith(tls_identity._SIGNATURE_PREFIX))
        self.assertFalse(
            set(payload[:len(tls_identity._SIGNATURE_PREFIX)]) <= set("0123456789abcdef")
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CertificateBindingTests(unittest.TestCase):
    def test_certificate_proves_this_node_identity(self):
        self.assertEqual(peer_id_from_certificate(_certificate_der()), get_node_public_key_hex())

    def test_certificate_without_the_extension_is_refused(self):
        with self.assertRaises(CertificateError):
            peer_id_from_certificate(_certificate_with_extensions([]))

    def test_extension_cannot_be_lifted_onto_another_certificate(self):
        # The attack the SubjectPublicKeyInfo signature exists to stop: copy a real
        # node's extension verbatim into a certificate holding *our* key, and the
        # signature no longer covers the key being presented.
        stolen = x509.load_der_x509_certificate(
            _certificate_der()
        ).extensions.get_extension_for_oid(HOST_KEY_EXTENSION_OID)
        forged = _certificate_with_extensions([stolen.value])
        with self.assertRaises(CertificateError):
            peer_id_from_certificate(forged)

    def test_non_canonical_public_key_is_refused(self):
        # An id must have exactly one spelling, or one node becomes several peer rows.
        _, signature = (
            x509.load_der_x509_certificate(_certificate_der())
            .extensions.get_extension_for_oid(HOST_KEY_EXTENSION_OID)
            .value.value.decode("ascii")
            .split(":")
        )
        uppercased = f"{get_node_public_key_hex().upper()}:{signature}".encode("ascii")
        with self.assertRaises(CertificateError):
            peer_id_from_certificate(
                _certificate_with_extensions([
                    x509.UnrecognizedExtension(HOST_KEY_EXTENSION_OID, uppercased)
                ])
            )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.server, self.target = _serve(grpc_transport.server_credentials())
        self.addCleanup(self.server.stop, 0)

    def test_channel_pinned_to_our_own_identity_round_trips(self):
        channel = grpc_transport.node_channel(
            self.target, expected_peer_id=get_node_public_key_hex()
        )
        self.addCleanup(channel.close)
        self.assertEqual(_echo(channel), b"ping")

    def test_first_contact_learns_the_identity_it_reached(self):
        channel, peer_id = grpc_transport.channel_and_peer_id(self.target)
        self.addCleanup(channel.close)
        self.assertEqual(peer_id, get_node_public_key_hex())
        self.assertEqual(_echo(channel), b"ping")

    def test_another_peer_id_at_this_address_is_refused(self):
        # An ISP-reassigned address, or an impostor on a stored one: it answers, it
        # proves an identity, and it is still not the peer we meant to reach. The
        # expected id has to be a well-formed one, or the refusal would come from
        # parsing it rather than from the certificate not matching.
        other_peer_id = "ab" * 32
        self.assertEqual(normalize_public_key_hex(other_peer_id), other_peer_id)
        with self.assertRaises(CertificateError):
            grpc_transport.node_channel(self.target, expected_peer_id=other_peer_id)

    def test_local_channel_verifies_the_node_it_reaches(self):
        port = int(self.target.rsplit(":", 1)[1])
        channel = grpc_transport.local_channel(port)
        self.addCleanup(channel.close)
        self.assertEqual(_echo(channel), b"ping")

    def test_a_plaintext_server_cannot_be_dialled(self):
        # The migration has no fallback on purpose: a node that has not moved to TLS is
        # unreachable rather than reachable in the clear.
        plaintext, target = _serve()
        self.addCleanup(plaintext.stop, 0)
        with self.assertRaises((ssl.SSLError, OSError)):
            grpc_transport.channel_and_peer_id(target)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TargetParsingTests(unittest.TestCase):
    def test_splits_ipv4_ipv6_and_names(self):
        self.assertEqual(grpc_transport.split_target("1.2.3.4:8080"), ("1.2.3.4", 8080))
        self.assertEqual(grpc_transport.split_target("[2001:db8::1]:8080"), ("2001:db8::1", 8080))
        self.assertEqual(grpc_transport.split_target("node.example:8080"), ("node.example", 8080))

    def test_rejects_targets_with_no_port(self):
        # A portless target would otherwise reach socket.create_connection as a name.
        for target in ("1.2.3.4", "", "1.2.3.4:", "1.2.3.4:http"):
            with self.assertRaises(ValueError):
                grpc_transport.split_target(target)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DualPortTests(unittest.TestCase):
    """One servicer on two ports: TLS for peers and the CLI, plain gRPC for services."""

    def setUp(self):
        self.server, self.tls_target, self.plain_target = _serve(
            grpc_transport.server_credentials(), plaintext_too=True
        )
        self.addCleanup(self.server.stop, 0)

    def test_both_ports_reach_the_same_servicer(self):
        verified = grpc_transport.node_channel(
            self.tls_target, expected_peer_id=get_node_public_key_hex()
        )
        self.addCleanup(verified.close)
        plain = grpc.insecure_channel(self.plain_target)
        self.addCleanup(plain.close)

        self.assertEqual(_echo(verified, b"over-tls"), b"over-tls")
        self.assertEqual(_echo(plain, b"in-the-clear"), b"in-the-clear")

    def test_the_tls_port_is_not_reachable_in_the_clear(self):
        # The offer is symmetric only from the caller's side: a plaintext client on the
        # TLS port must still fail, or "peers always get TLS" would not hold.
        channel = grpc.insecure_channel(self.tls_target)
        self.addCleanup(channel.close)
        with self.assertRaises(grpc.RpcError):
            _echo(channel, timeout=5)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServiceFacingGatewayTests(unittest.TestCase):
    """What `__config__.gateway` hands a service we execute."""

    def _peer(self):
        peer = celaut_pb2.Peer()
        peer.uri.add(ip="192.168.200.1", port=6000)
        return peer

    def test_a_service_is_given_the_plaintext_port(self):
        # A service speaks plain gRPC and learns this address as data, so there is
        # nothing for it to guess and nothing to pin.
        with unittest.mock.patch.object(
            gateway_utils, "_plaintext_gateway_port", lambda: 6001
        ), unittest.mock.patch.object(gateway_utils, "_gateway_port", lambda: 6000):
            instance = gateway_utils.peer_gateway_instance(self._peer())

        self.assertEqual([slot.port for slot in instance.api.slot], [6001])
        self.assertEqual(
            [(u.ip, u.port) for s in instance.uri_slot for u in s.uri],
            [("192.168.200.1", 6001)],
        )

    def test_falls_back_to_the_tls_port_when_plaintext_is_disabled(self):
        # Then a service does have to speak TLS -- but nothing is left pointing at a
        # port the node does not serve.
        with unittest.mock.patch.object(
            gateway_utils, "_plaintext_gateway_port", lambda: 0
        ), unittest.mock.patch.object(gateway_utils, "_gateway_port", lambda: 6000):
            instance = gateway_utils.peer_gateway_instance(self._peer())

        self.assertEqual([slot.port for slot in instance.api.slot], [6000])
        self.assertEqual(
            [(u.ip, u.port) for s in instance.uri_slot for u in s.uri],
            [("192.168.200.1", 6000)],
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PlaintextGatewayBindTests(unittest.TestCase):
    """Where the plain-gRPC port listens.

    The same `Gateway` is behind it, with nothing authenticating the caller, so the
    address it binds *is* the security boundary: everything the TLS port is for would be
    handed away by a listener on every interface.
    """

    def test_binds_the_gateway_address_from_the_config_file(self):
        # The proto contract decides: the same setting that fills __config__.gateway,
        # so a service reaches the port exactly where it was told to look.
        with unittest.mock.patch.object(
            gateway_utils, "_uri_for_network",
            return_value=celaut_pb2.Instance.Uri(ip="192.168.200.1", port=6000),
        ) as resolved:
            self.assertEqual(gateway_utils.plaintext_gateway_host(), "192.168.200.1")

        self.assertEqual(
            resolved.call_args.args,
            (gateway_utils.env_manager.get("virtualizers.ch.NETWORK_BRIDGE_NAME"),),
        )

    def test_never_binds_every_interface(self):
        # Neither branch out of here may widen into a wildcard: an interface that
        # somehow reports one is refused rather than served.
        for reported in ("0.0.0.0", "::", ""):
            with self.subTest(reported=reported):
                with unittest.mock.patch.object(
                    gateway_utils, "_uri_for_network",
                    return_value=celaut_pb2.Instance.Uri(ip=reported, port=6000),
                ):
                    self.assertEqual(
                        gateway_utils.plaintext_gateway_host(), "127.0.0.1"
                    )

    def test_falls_back_to_loopback_when_the_bridge_is_not_up(self):
        # A fresh install has no bridge yet. The local hop still works and the kernel,
        # not an ACL nobody wrote, refuses everyone else.
        with unittest.mock.patch.object(
            gateway_utils, "_uri_for_network", side_effect=Exception("no such interface")
        ):
            self.assertEqual(gateway_utils.plaintext_gateway_host(), "127.0.0.1")
