"""This node's TLS identity (issue #257).

Every gRPC hop used to be plaintext, so a peer could prove *what it said* (the signed
``Peer`` of issue #236) but never *who it was talking to*: an active MITM could sit
between two nodes and swap traffic. This module gives the node a TLS certificate tied
to the identity key that already is its ``peer_id``, so the channel authenticates the
same fact the application layer does -- with no CA, no PKI and no system trust store
(see ``grpc_transport``, which pins the certificate explicitly).

The certificate does not hold the identity key itself. It holds a throwaway P-256 key,
and an X.509 extension carries the node's identity public key plus a signature over
that certificate's own ``SubjectPublicKeyInfo`` -- the indirection libp2p uses
(https://github.com/libp2p/specs/blob/master/tls/tls.md). Verifying the extension
proves the holder of the identity key authorised this certificate, so a bare
``ip:port`` can be confirmed to be the peer we meant to reach with no previously
received ``Peer`` message and no trust-on-first-use.

The indirection is what the design wants regardless of which cryptography the identity
is in: the identity key never enters a handshake, never sits in the memory of the
process doing one, and could one day live offline or in an HSM, while the key that does
the negotiating stays disposable. It also happens to be the only option available --
the ``grpcio`` wheels ship BoringSSL, whose ``ec.h`` only knows the NIST curves, so a
secp256k1 certificate is impossible rather than merely unnegotiated -- but that is the
lesser reason, and it is not the one to reach for when the identity scheme changes.

The extension format is ours, not libp2p's: their ``peer_id`` is a multihash of a
protobuf-encoded key while ours is the raw public key, so reusing their IANA-allocated
OID while diverging on what it contains would be mislabelling. The OID below lives under
the UUID arc ``2.25`` (ITU-T X.667), which anyone may derive from a UUID without
registering anything.

That OID and the signed payload's prefix are **protocol constants of the ``[tls, grpc]``
stack**, not implementation details: a peer finds the extension by the OID and
recomputes the payload to verify it, so both have to match byte for byte or the
handshake is refused. A node that changes either speaks a different transport and must
declare it as such in ``Peer.Uri.protocol_stack`` rather than announcing
``["tls", "grpc"]``.

The P-256 key is generated per process and never touches disk. Trust comes from the
extension, not from the certificate being stable, so there is nothing to persist,
rotate or back up -- and no second private key on the filesystem.

Nothing here is specific to the identity's signature scheme. The payload signed, the
extension's ``<public key hex>:<signature hex>`` encoding and the verification path all
go through ``node_identity`` (``sign_peer_payload`` / ``verify_peer_payload`` /
``normalize_public_key_hex``) and read both halves as hex of whatever length that
module defines, so an identity in a different curve or algorithm changes what those
functions do internally and leaves this format untouched.
"""
import datetime
from functools import lru_cache
from typing import Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from src.reputation_system.node_identity import (
    get_node_public_key_hex,
    normalize_public_key_hex,
    sign_peer_payload,
    verify_peer_payload,
)

# The name every node's certificate is issued to. Certificates are pinned by their
# exact bytes, so the hostname carries no information -- but BoringSSL still checks it,
# and the addresses a node is reached at are IP literals with no name of their own. A
# single constant, overridden on the channel via `grpc.ssl_target_name_override`, keeps
# the check satisfied without inventing a name per address.
TLS_SERVER_NAME = "celaut-node"

# Protocol constants of the `[tls, grpc]` stack. Both are values every peer speaking it
# must agree on byte for byte, and neither is a local choice:
#
#   * a peer looks the extension up BY the OID, so two nodes carrying different ones
#     simply do not find each other's, and the handshake is refused;
#   * a verifier recomputes the signed payload from the certificate it received, so a
#     different prefix makes every signature fail to verify.
#
# A node that changes either is speaking a different transport protocol, not a variant
# of this one, and has to say so: `Peer.Uri.protocol_stack` is where an address declares
# what it speaks, and it must then carry tags other than `["tls", "grpc"]`. Announcing
# that stack while using other constants is mislabelling, exactly as reusing libp2p's
# OID for our own contents would be.
#
# uuid.uuid5(uuid.NAMESPACE_OID, "CELAUT") as an integer, under the ITU-T X.667 UUID arc
# -- an OID anyone may derive from a UUID without registering anything. The seed is the
# project name and nothing else, so the value can be recomputed in one line and audited.
# It is a name seed, never a resource: nothing resolves it, and only the number below
# travels. Frozen here rather than derived at import, which would hide a constant behind
# a computation, and it must never change once peers are speaking it.
HOST_KEY_EXTENSION_OID = x509.ObjectIdentifier(
    "2.25.276125094420857322236898758448456352855"
)

# Domain separation for the extension's signature, so a signature made here can never be
# replayed as a signed `Peer` payload (or the other way round): both are signed with the
# same identity key. What keeps them apart is that `canonical_peer_payload` starts with a
# lowercase public key hex, which cannot begin with these bytes -- so any later payload
# signed by the identity key must be checked against this one before it is introduced.
_SIGNATURE_PREFIX = "CELAUT"

# X.509 requires a validity window, so a node with a skewed clock could reject a
# legitimate peer over a field that carries no security here (the pinning does the
# work). The window is therefore as wide as the format allows.
_NOT_VALID_BEFORE = datetime.datetime(2000, 1, 1)
_NOT_VALID_AFTER = datetime.datetime(9999, 12, 31)


class CertificateError(Exception):
    """A certificate does not prove possession of the identity key it claims."""


def _host_key_payload(certificate_spki: bytes) -> str:
    """The string the identity key signs to authorise ``certificate_spki``.

    Signing the SubjectPublicKeyInfo -- not the whole certificate -- is what lets the
    signature be produced before the certificate exists, and what makes the binding
    survive anything else in the certificate changing.

    The payload names no curve, no algorithm and no key length: it is a domain-separated
    prefix over the SPKI's own bytes. Which cryptography signs it is
    ``sign_peer_payload``'s business, and which one verifies it is
    ``verify_peer_payload``'s, so changing the node's identity scheme does not reopen
    this format.
    """
    return _SIGNATURE_PREFIX + certificate_spki.hex()


def _build_certificate(public_key_hex: str) -> Tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    signature = sign_peer_payload(_host_key_payload(spki))
    if not signature:
        raise CertificateError("This node has no identity key to bind a certificate to.")

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, TLS_SERVER_NAME)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOT_VALID_BEFORE)
        .not_valid_after(_NOT_VALID_AFTER)
        # The certificate is its own trust anchor: a client pins it as
        # `root_certificates`, and BoringSSL will only accept a root that says it is
        # one. libp2p's certificates omit this because they verify in a handshake
        # callback, which gRPC Python does not expose.
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(TLS_SERVER_NAME)]), critical=False
        )
        # `public_key_hex:signature_hex` in ASCII. The OID is ours, so the contents are
        # ours to define, and two colon-separated hex fields need no ASN.1 of their own
        # -- neither side is read by length, so neither constrains the identity scheme.
        .add_extension(
            x509.UnrecognizedExtension(
                HOST_KEY_EXTENSION_OID, f"{public_key_hex}:{signature}".encode("ascii")
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


@lru_cache(maxsize=1)
def certificate_and_key() -> Tuple[bytes, bytes]:
    """This process' ``(certificate_pem, private_key_pem)``, generated on first use.

    Cached because every peer that dials this node must be shown the same certificate
    it verified during the pre-flight: regenerating one per connection would fail the
    pinning that the whole scheme rests on.
    """
    public_key_hex = get_node_public_key_hex()
    if not public_key_hex:
        raise CertificateError(
            "This node has no identity keypair (ledgers.ergo.WALLET_MNEMONIC is unset), "
            "so it cannot serve or dial TLS. Load the config to generate one."
        )
    return _build_certificate(public_key_hex)


def peer_id_from_certificate(certificate_der: bytes) -> str:
    """The ``peer_id`` a certificate proves, or raise ``CertificateError``.

    This is the whole verification: an unknown ``ip:port`` that answers with a
    certificate carrying a valid signature over its own public key is provably held by
    the node owning that identity key, whatever else it claims about itself.
    """
    try:
        certificate = x509.load_der_x509_certificate(certificate_der)
    except Exception as e:
        raise CertificateError(f"Unparseable certificate: {e}")

    try:
        extension = certificate.extensions.get_extension_for_oid(HOST_KEY_EXTENSION_OID)
    except x509.ExtensionNotFound:
        raise CertificateError("Certificate carries no celaut host-key extension.")

    try:
        public_key_hex, _, signature = extension.value.value.decode("ascii").partition(":")
    except UnicodeDecodeError:
        raise CertificateError("Host-key extension is not ASCII.")

    if normalize_public_key_hex(public_key_hex) != public_key_hex:
        raise CertificateError("Host-key extension holds a non-canonical public key.")

    spki = certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if not verify_peer_payload(public_key_hex, _host_key_payload(spki), signature):
        raise CertificateError(
            f"Certificate is not signed by the identity key {public_key_hex} it claims."
        )
    return public_key_hex


def certificate_pem(certificate_der: bytes) -> bytes:
    """PEM form of a DER certificate, which is what gRPC's credentials take."""
    return x509.load_der_x509_certificate(certificate_der).public_bytes(
        serialization.Encoding.PEM
    )
