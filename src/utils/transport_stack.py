"""What ``["tls", "grpc"]`` actually means, written down (issue #257).

An address announces the stack it speaks in ``Peer.Uri.protocol_stack``, and a tag on
its own says almost nothing: two nodes can both write ``tls`` and disagree on the
extension OID, on what the signature covers, or on which RPCs exist, and neither would
be able to tell from the announcement. celaut has no conventions to fall back on -- the
proto is where a thing is defined -- so a node that announces this stack has to say what
it means by it.

Each component is declared the way every replaceable component in celaut is:

``tags``
    The plain name of the protocol -- ``tls``, ``grpc``. What a reader looks for, and
    deliberately not a versioned label: a variant is not a new protocol, it is the same
    protocol with different parameters, and those belong in the fields below.

``formal``
    The parameters, as canonical ``key=value`` lines sorted by key. This is what decides
    a comparison (:func:`node_identity._same_component` reads ``formal`` first), so any
    difference that would stop two nodes from talking -- a different OID, a different
    signed payload, an added or removed RPC -- shows up as different bytes here. Machine
    -readable and small enough to travel in an Ergo register.

``prose``
    The same thing written out for a person, with the detail an implementer needs. Nodes
    do not compare it: agreeing that two differently-worded descriptions mean the same
    protocol is a judgement, and the shape of the service that could make it is
    ``(a, b) -> bool`` over the two texts -- an LLM's job, not a node's. It travels so
    the descriptor can be read, not to be diffed.

The gRPC half is derived from the compiled descriptor rather than typed out, so adding
or removing an RPC changes what this node announces without anyone remembering to.
"""
from typing import Dict, Iterable, Tuple

from protos import celaut_pb2
from src.reputation_system.node_identity import same_component_stack
from src.utils.tls_identity import (
    HOST_KEY_EXTENSION_OID,
    TLS_SERVER_NAME,
    signature_prefix,
)

# The gRPC service a node serves on this stack. Named here rather than assumed, because
# `formal` below is built from whatever the compiled proto says it contains.
GATEWAY_SERVICE_NAME = "Gateway"


def _formal(pairs: Dict[str, str]) -> bytes:
    """``key=value`` lines, sorted by key, UTF-8.

    Sorted so two nodes that declare the same parameters produce identical bytes
    whatever order they built them in -- this value is compared byte for byte, and it is
    covered by the announcement's signature, so it must not depend on iteration order.
    """
    return "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs)).encode("utf-8")


def _gateway_methods() -> str:
    """Every RPC of the Gateway service, sorted, from the compiled descriptor.

    Read from the descriptor instead of listed here so the declaration cannot drift from
    the service actually served: a node that adds an RPC announces a different stack
    from one that has not, which is exactly the difference a caller needs to see.
    """
    service = celaut_pb2.DESCRIPTOR.services_by_name[GATEWAY_SERVICE_NAME]
    return ",".join(sorted(method.name for method in service.methods))


def tls_component() -> Tuple[Tuple[str, ...], str, bytes]:
    """The ``tls`` half: how a caller authenticates the address it dialled."""
    formal = _formal({
        "min_version": "1.3",
        "server_name": TLS_SERVER_NAME,
        "certificate": "self-signed,p-256,ca:true",
        "client_auth": "none",
        "host_key_oid": HOST_KEY_EXTENSION_OID.dotted_string,
        "host_key_extension": "ascii:<identity_public_key_hex>:<signature_hex>",
        "host_key_signed": f"{signature_prefix()}<subject_public_key_info_der_hex>",
        "verification": "read-certificate,verify-extension,pin-exact-certificate",
    })
    prose = (
        "TLS 1.3 with a self-signed certificate, and no CA, PKI or system trust store. "
        "The certificate holds a P-256 key generated per process and never written to "
        "disk; it is its own trust anchor (CA:TRUE) because a caller pins it as the "
        "channel's only root. It is issued to the fixed name above, which carries no "
        "information -- certificates are pinned by their exact bytes -- and exists only "
        "because the addresses a node is reached at are IP literals with no name. "
        "An X.509 extension under the OID above, non-critical, carries "
        "'<identity public key hex>:<signature hex>' in ASCII. The signature is made "
        "with the node's identity key over the prefix above followed by the hex of the "
        "certificate's own SubjectPublicKeyInfo in DER -- not over the whole "
        "certificate, so it can be produced before the certificate exists and survives "
        "any other field changing. Verifying it proves the holder of the identity key "
        "authorised this certificate, which is what lets a bare ip:port be confirmed to "
        "belong to a given peer with no prior message and no trust-on-first-use. "
        "A caller reads the certificate first, checks that extension itself, and only "
        "then opens the channel pinned to that exact certificate, so anything else "
        "presented afterwards fails. The client is not asked for a certificate: it "
        "proves who it is at the application layer, with the signature on its own "
        "announcement."
    )
    return ("tls",), prose, formal


def grpc_component() -> Tuple[Tuple[str, ...], str, bytes]:
    """The ``grpc`` half: which RPCs the address answers, and how they are framed."""
    formal = _formal({
        "transport": "http/2",
        "alpn": "h2",
        "service": celaut_pb2.DESCRIPTOR.services_by_name[GATEWAY_SERVICE_NAME].full_name,
        "methods": _gateway_methods(),
        "signature": "stream buffer.Buffer -> stream buffer.Buffer",
        "framing": "bee-rpc",
    })
    prose = (
        "gRPC over HTTP/2, negotiated with ALPN 'h2'. The address answers the service "
        "named above and the RPCs listed with it, and nothing else. Every one of them "
        "is a bidirectional stream of buffer.Buffer in both directions -- the request "
        "and response types a reader would expect are carried inside that stream by "
        "bee-rpc, which frames a message into blocks so a large one need not be held "
        "whole in memory at either end. So the method set, not the message types, is "
        "what distinguishes one node's gateway from another's: a node serving a "
        "different set of RPCs speaks a different protocol on this address, whatever "
        "its tags say."
    )
    return ("grpc",), prose, formal


def declare_transport_stack(uri, *, prose: bool = True) -> None:
    """Declare on ``uri`` the stack its address speaks, replacing anything already there.

    ``prose=False`` drops the descriptions, for the same reason
    ``node_identity.declare_signature_scheme`` does: an announcement published to an Ergo
    register pays storage rent on every byte forever, and these two paragraphs are far
    more than a register's whole budget. Nothing is lost from a *verification*, because
    what a comparison reads is ``formal`` and the tags -- prose was never part of that
    decision. What is lost is a reader's ability to learn the protocol from the
    announcement alone, which is why it is kept everywhere the size is not paid for.
    """
    del uri.protocol_stack[:]
    for tags, description, formal in (tls_component(), grpc_component()):
        uri.protocol_stack.add(
            tags=list(tags), prose=description if prose else "", formal=formal
        )


def node_transport_stack(*, prose: bool = True):
    """This node's stack as a detached list of ``Peer.Uri.Protocol`` messages."""
    uri = celaut_pb2.Peer.Uri()
    declare_transport_stack(uri, prose=prose)
    return list(uri.protocol_stack)


def speaks_our_transport_stack(protocol_stack: Iterable) -> bool:
    """Whether an announced stack is the one this node speaks.

    The same comparison a signature scheme gets (``node_identity.same_component_stack``):
    ``formal`` decides, an exact set of tags decides when neither side declares one, and
    the pairing across the stack must be total. So a peer announcing ``tls`` and ``grpc``
    with a different OID, a different signed payload or a different set of RPCs is
    correctly seen as speaking something else, while one that only worded its prose
    differently is not.

    An empty stack means the sender said nothing rather than that it speaks nothing:
    announcements predating this declaration carry no components, and the tags they
    would have carried are the ones this node speaks anyway.
    """
    components = list(protocol_stack)
    if not components:
        return True
    return same_component_stack(components, node_transport_stack())
