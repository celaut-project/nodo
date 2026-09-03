"""The one way this node opens a gRPC channel or serves one (issue #257).

Every channel opened here is TLS, and every certificate is verified against the
``peer_id`` it belongs to before a single byte of gRPC is spoken. There is no plaintext
path through this module at all -- not for peers, not for the local CLI, which reach
the gateway on the same TLS port, so an exception for loopback would only mean a second
policy to keep in step with the first.

The node does serve that same gateway in plain gRPC on a second port
(``network.GATEWAY_PLAINTEXT_PORT``), for the services it executes and for external
callers that decline TLS. Nothing here can dial it and it is never announced: TLS is
what the node offers, plaintext is what it tolerates.

Verification cannot happen during the handshake, because gRPC Python exposes no
certificate-verification callback (grpc/grpc#10701, closed as stale; still absent in
1.83). So it happens in two steps:

1. **Pre-flight with the stdlib.** ``ssl`` hands us the peer's certificate with
   ``getpeercert(binary_form=True)``, and we check its host-key extension ourselves --
   the freedom of verification ``grpcio`` does not give us.
2. **Pin exactly what we just verified** as the channel's ``root_certificates``. This
   closes the gap between check and use: if the server then presents anything else, the
   channel fails. It also means no CA, no ACME/OCSP/CT and no system trust store --
   passing ``root_certificates`` explicitly keeps ``ca-certificates``/``certifi`` out of
   the picture. The root of trust is the peer's own identity key.

mTLS was ruled out rather than skipped: ``require_client_auth=True`` needs
``root_certificates`` that chain, so a first contact (``GetPeerInfo`` /
``GenerateClient`` from a node we have never seen) could never complete the handshake
(grpc/grpc#16547, also closed as stale). The client therefore authenticates at the
application layer, which is what the signed ``Peer`` of issue #236 already does.

No channel caching yet, by design: pooling brings URI invalidation -- which interacts
with multi-address peers -- plus dead-channel handling and concurrency. The cost is
visible (a pre-flight handshake plus the gRPC one per call, and ``metrics`` is polled
per instance from ``maintain``), so it is worth measuring with TLS in place and then
designing the pool, rather than guessing now.
"""
import socket
import ssl
from typing import Optional, Tuple

import grpc

from src.reputation_system.node_identity import (
    get_node_public_key_hex,
    normalize_public_key_hex,
)
from src.utils.config import ConfigManager
from src.utils.tls_identity import (
    TLS_SERVER_NAME,
    CertificateError,
    certificate_and_key,
    certificate_pem,
    peer_id_from_certificate,
)
from src.utils.utils import format_uri, generate_uris_by_peer_id

# The pre-flight is a TLS handshake against an address a caller already believes is
# reachable, so this only has to cover a slow link, not a dead one.
PREFLIGHT_TIMEOUT_S = 5.0

# Certificates are pinned by their bytes, so the name in them is fixed and meaningless
# (see tls_identity.TLS_SERVER_NAME) -- but BoringSSL checks it against the target,
# which is an ip:port. Overriding it is what makes an IP-addressed peer verifiable.
_CHANNEL_OPTIONS = (("grpc.ssl_target_name_override", TLS_SERVER_NAME),)


def split_target(target: str) -> Tuple[str, int]:
    """``host, port`` of a gRPC target, including ``[::1]:8080`` for IPv6 literals."""
    host, _, port = str(target).rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"Not a host:port gRPC target: {target!r}")
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host, int(port)


def _fetch_certificate(target: str) -> bytes:
    """The certificate ``target`` serves, unverified, over a throwaway TLS connection.

    ALPN advertises ``h2`` because that is what the server is: gRPC's C-core negotiates
    HTTP/2 and can refuse a client that offers nothing.
    """
    host, port = split_target(target)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Nothing is trusted here on purpose: this connection exists only to read the
    # certificate, and it is judged by its host-key extension, not by a chain or a name.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(["h2"])
    with socket.create_connection((host, port), timeout=PREFLIGHT_TIMEOUT_S) as sock:
        with context.wrap_socket(sock, server_hostname=TLS_SERVER_NAME) as tls:
            certificate = tls.getpeercert(binary_form=True)
    if not certificate:
        raise CertificateError(f"{target} presented no certificate.")
    return certificate


def channel_and_peer_id(target: str) -> Tuple[grpc.Channel, str]:
    """A verified channel to ``target`` and the ``peer_id`` it turned out to belong to.

    For first contact, where the id is what we are trying to learn (``nodo connect
    <ip:port>``): the certificate proves its own identity key, so this is not
    trust-on-first-use -- whatever answers is either provably a node's own address or
    refused.
    """
    certificate = _fetch_certificate(target)
    peer_id = peer_id_from_certificate(certificate)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=certificate_pem(certificate)
    )
    return grpc.secure_channel(target, credentials, options=list(_CHANNEL_OPTIONS)), peer_id


def node_channel(target: str, expected_peer_id: str) -> grpc.Channel:
    """A verified channel to ``target``, refused unless it is ``expected_peer_id``.

    Whoever answers at a stored address is not necessarily the peer we meant to reach:
    an ISP can reassign it, and until now nothing checked. This is where that check
    lives, so callers no longer have to treat an answer as evidence of an identity.
    """
    expected = normalize_public_key_hex(expected_peer_id)
    if not expected:
        raise ValueError(f"Not a peer id: {expected_peer_id!r}")
    channel, peer_id = channel_and_peer_id(target)
    if peer_id != expected:
        channel.close()
        raise CertificateError(
            f"{target} is held by {peer_id}, not by the expected peer {expected}."
        )
    return channel


def verified_channel(target: str, expected_peer_id: Optional[str] = None) -> grpc.Channel:
    """A verified channel to a caller-supplied address.

    ``expected_peer_id`` is optional because some addresses arrive from a person
    (``nodo tunnel --peer 1.2.3.4:8090``) with no id attached. Even then the certificate
    still has to prove *some* identity key, so an address that answers with anything
    else -- a plaintext service, a proxy, a node that predates this -- is refused rather
    than silently accepted.
    """
    if expected_peer_id:
        return node_channel(target, expected_peer_id=expected_peer_id)
    return channel_and_peer_id(target)[0]


def peer_channel(peer_id: str) -> grpc.Channel:
    """A verified channel to one of ``peer_id``'s known addresses."""
    uri = next(generate_uris_by_peer_id(peer_id=peer_id), None)
    if not uri:
        raise ConnectionError(f"No reachable address is known for peer {peer_id}.")
    return node_channel(uri, expected_peer_id=peer_id)


def local_channel(port: Optional[int] = None) -> grpc.Channel:
    """A verified channel to this node's own gateway, for the CLI and local callers.

    Local traffic gets the same treatment as everything else -- and it verifies against
    our own public key, so a CLI cannot be tricked into driving somebody else's node
    through a hijacked local port.
    """
    public_key = get_node_public_key_hex()
    if not public_key:
        raise CertificateError(
            "This node has no identity keypair, so its own gateway cannot be verified."
        )
    gateway_port = port if port is not None else ConfigManager().get_gateway_port()
    return node_channel(
        format_uri("127.0.0.1", int(gateway_port)), expected_peer_id=public_key
    )


def server_credentials() -> grpc.ServerCredentials:
    """The credentials the gateway listens with: this node's certificate.

    Server-side only. A client is not asked for a certificate (see the module docstring
    on mTLS) -- it proves who it is with the application-layer signatures of issue #236.
    """
    certificate, private_key = certificate_and_key()
    return grpc.ssl_server_credentials([(private_key, certificate)])
