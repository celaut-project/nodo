"""TLS on every gRPC hop, pinned to the node's identity key (issue #257).

What these pin, in order of what would hurt most if it broke:

* the certificate is worthless unless its extension is signed over *its own* public
  key -- otherwise the extension could be lifted from a legitimate node's certificate
  and stapled onto an attacker's, which is the whole attack the binding prevents;
* an address that answers with a certificate for a different identity is refused, which
  is the check that did not exist while every channel was plaintext;
* the server the node actually serves with, and the channels the node actually dials
  with, talk to each other end to end -- the spike in the issue was a deduction, and
  the parts it deduced (a stdlib pre-flight against a gRPC server, a `CA:TRUE`
  self-signed certificate accepted as its own trust anchor) are exercised here.
"""
import datetime
import ssl
import unittest
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
    from src.reputation_system.node_identity import get_node_public_key_hex
    from src.utils import grpc_transport
    from src.utils.tls_identity import (
        HOST_KEY_EXTENSION_OID,
        TLS_SERVER_NAME,
        CertificateError,
        certificate_and_key,
        peer_id_from_certificate,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def _serve(credentials=None):
    """A one-method gRPC server, TLS unless ``credentials`` is None. Returns (server, target)."""
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
    server.start()
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
        # proves an identity, and it is still not the peer we meant to reach.
        with self.assertRaises(CertificateError):
            grpc_transport.node_channel(self.target, expected_peer_id="02" + "ab" * 32)

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
