"""What an address speaks, written down layer by layer (issue #257).

An address announces the stack it speaks in ``Peer.Uri.protocol_stack``, and a tag on
its own says almost nothing: two nodes can both write ``tls`` and disagree on the
extension OID, on what the signature covers, or on which RPCs exist, and neither would
be able to tell from the announcement. celaut has no conventions to fall back on -- the
proto is where a thing is defined -- so a node that announces a stack has to say what
it means by it, completely: everything two nodes must have agreed on to talk is in
it, and nothing that each one chooses for itself is. The port an address listens on
is the example of the second kind: every peer picks its own, so it is in
``Peer.Uri.port`` and never in a layer. So is anything a receiver accepts in any
variant -- the size of a bee-rpc chunk, say -- since a sender is then free to choose.

The stack is ordered, bottom to top, one entry per layer of abstraction:

    tls -> http2 -> grpc -> bee-rpc -> celaut-gateway

on top of ``Peer.Uri.transport`` (tcp), which is declared the same way in its own
field. Each entry is declared the way every replaceable component in celaut is:

``tags``
    The plain name of the protocol -- ``tls``, ``grpc``. What a reader looks for, and
    deliberately not a versioned label: a variant is not a new protocol, it is the same
    protocol with different parameters, and those belong in the fields below.

``formal``
    The parameters, as canonical ``key=value`` lines sorted by key
    (``node_identity.component_formal``). This is what decides a comparison, so any
    difference that would stop two nodes from talking -- a different OID, a different
    signed payload, a field number with another type, an RPC that carries other
    messages -- shows up here. Complete, not a pointer: the celaut-gateway and bee-rpc
    layers carry their whole message schema (``protocol_schema``), and every signed
    payload is written out with its prefix.

``prose``
    The same thing written out for a reader, in ASD-STE100 Simplified Technical
    English: short sentences, one instruction per sentence, active voice. A reader
    holding only this announcement -- off a gRPC response -- cannot follow a link into
    a repository, so the text has to stand on its own. Nodes do not compare it:
    agreeing that two differently-worded descriptions mean the same protocol is a
    judgement, and the shape of the service that could make it is ``(a, b) -> bool``
    over the two texts -- an LLM's job, not a node's. It travels so the descriptor can
    be read, not to be diffed, and only where the operator decides it is worth the
    bytes (``communication.SHARE_PROSE_ON_*``). ``nodo protocol`` prints it always.

Whatever can be derived from the code is: the RPC tables from
``protos.gateway_bee.GATEWAY_RPCS`` (which the server and the client also read), the
message schema from the compiled descriptors, the TLS constants from ``tls_identity``,
the signed payloads from the functions that build them. Changing any of those changes
what this node announces without anyone remembering to.

Comparison is positional (:func:`compatible_layer_stacks`): layer ``i`` against layer
``i``. There are no version numbers: two layers are compatible or they are not
(:func:`formal_conflicts`). Most keys must be equal on both sides. The ``schema.*`` and
``rpc.*`` keys follow protobuf and gRPC instead. A message field or an RPC that only
one side declares does not stop the two nodes from talking, because a protobuf reader
ignores a field it does not know, and a node does not call an RPC it does not know.
What both sides declare must agree. There is no fallback for a node that declares
something else, or nothing: it does not speak this protocol, and it is not spoken to.
"""
import hashlib
from typing import Dict, Final, Iterable, List, Optional, Tuple

from bee_rpc import buffer_pb2
from bee_rpc.utils import Enviroment

from protos import celaut_pb2
from protos.gateway_bee import AUTH_KINDS, GATEWAY_RPCS
from src.identity.node_identity import (
    ComponentFormalError,
    attestation_payload,
    canonical_peer_payload,
    client_binding_payload,
    component_formal,
    parse_component_formal,
    same_component,
)
from src.identity import protocol_doc
from src.identity.protocol_schema import schema_pairs
from src.utils.config import ConfigManager
from src.identity.tls_identity import (
    HOST_KEY_EXTENSION_OID,
    TLS_SERVER_NAME,
    signature_prefix,
)

# The gRPC service a node serves on this stack. Named here rather than assumed, because
# `formal` below is built from whatever the compiled proto says it contains.
GATEWAY_SERVICE_NAME = "Gateway"

# Where the prose travels, decided per destination because the two destinations charge
# for it differently: gRPC bytes are transient, a ledger register's are rented forever.
SHARE_PROSE_ON_GET_PEER_INFO_KEY: Final[str] = (
    "communication.SHARE_PROSE_ON_GET_PEER_INFO"
)
SHARE_PROSE_ON_LEDGER_KEY: Final[str] = "communication.SHARE_PROSE_ON_LEDGER"

Layer = Tuple[Tuple[str, ...], str, bytes]


# ---------------------------------------------------------------------------------
# Peer.Uri.transport
# ---------------------------------------------------------------------------------

def transport_component() -> Layer:
    """``tcp``: what ``Peer.Uri.transport`` names, under the whole stack."""
    formal = component_formal({
        "spec": "RFC9293",
        "port": "per-address",
    })
    prose = (
        "The transport is TCP, as RFC 9293 specifies. "
        "Each address gives one IP address and one port. "
        "The port is not part of the protocol. Each node selects its own port, "
        "and the address gives it."
    )
    return ("tcp",), prose, formal


# ---------------------------------------------------------------------------------
# Layer 1: tls
# ---------------------------------------------------------------------------------

def tls_component() -> Layer:
    """``tls``: how a caller authenticates the address it dialled."""
    formal = component_formal({
        "spec": "RFC8446",
        "min_version": "1.3",
        "alpn": "h2",
        "server_name": TLS_SERVER_NAME,
        "certificate": "self-signed,p-256,ca:true",
        "client_auth": "none",
        "host_key_oid": HOST_KEY_EXTENSION_OID.dotted_string,
        "host_key_critical": "false",
        "host_key_extension": "ascii:<identity_public_key_hex>:<signature_hex>",
        "host_key_signed": f"{signature_prefix()}<subject_public_key_info_der_hex>",
        "host_key_signature_scheme": "Peer.signature_scheme",
        "verification": "read-certificate,verify-extension,pin-exact-certificate",
    })
    prose = (
        "This layer is TLS 1.3, as RFC 8446 specifies. Do not use a lower version.\n"
        "\n"
        "The server sends one self-signed X.509 certificate. The client does not send "
        "a certificate. The certificate contains a P-256 public key. The certificate is "
        "its own certificate authority (CA:TRUE). There is no other certificate "
        "authority and no trust store.\n"
        "\n"
        f"The certificate gives the name \"{TLS_SERVER_NAME}\". This name gives no data "
        "about the node. The client sends this name in the TLS handshake. The client "
        "and the server use ALPN, and the only protocol name is \"h2\".\n"
        "\n"
        "The certificate contains one extension that is not critical. The formal field "
        "gives the OID of this extension. The value of the extension is ASCII text with "
        "two parts. A colon is between the two parts. The first part is the identity "
        "public key of the node. The second part is a signature. Both parts are in "
        "lowercase hexadecimal.\n"
        "\n"
        "The node makes the signature with its identity key, in the scheme that "
        f"Peer.signature_scheme gives. The signed data is the ASCII text \"{signature_prefix()}\", "
        "followed by the SubjectPublicKeyInfo of the certificate. The "
        "SubjectPublicKeyInfo is in DER, written in lowercase hexadecimal.\n"
        "\n"
        "To open a connection, do these steps:\n"
        "1. Open a TLS connection. Get the certificate of the server. Do not examine a "
        "certificate chain or a name.\n"
        "2. Find the extension. Make sure that the signature is correct for the public "
        "key in the extension. If it is not correct, close the connection.\n"
        "3. Make sure that this public key is the identity of the node that you want.\n"
        "4. Open the connection again. Accept only the same certificate, byte for byte."
    )
    return ("tls",), prose, formal


# ---------------------------------------------------------------------------------
# Layer 2: http2
# ---------------------------------------------------------------------------------

def http2_component() -> Layer:
    """``http2``: the framing gRPC runs on."""
    formal = component_formal({
        "spec": "RFC9113",
        "negotiation": "tls-alpn:h2",
    })
    prose = (
        "This layer is HTTP/2, as RFC 9113 specifies. The client selects HTTP/2 with "
        "ALPN \"h2\" in the TLS handshake. Do not use HTTP/1.1 or an HTTP upgrade."
    )
    return ("http2",), prose, formal


# ---------------------------------------------------------------------------------
# Layer 3: grpc
# ---------------------------------------------------------------------------------

def grpc_component() -> Layer:
    """``grpc``: how a call is laid out on HTTP/2."""
    formal = component_formal({
        "spec": "grpc/PROTOCOL-HTTP2",
        "path": "/<package>.<service>/<method>",
        "content_type": "application/grpc",
        "message_encoding": "identity",
        "metadata": "none",
        "call": "bidirectional-stream",
        "message": "buffer.Buffer",
        "error": "status-not-ok",
        "error_details": "human-readable",
    })
    prose = (
        "This layer is gRPC over HTTP/2, as the gRPC document \"PROTOCOL-HTTP2\" "
        "specifies.\n"
        "\n"
        "Each call uses the path \"/<package>.<service>/<method>\". The celaut-gateway "
        "layer gives the service and its methods. The content type is "
        "\"application/grpc\". The messages are not compressed. The calls send no "
        "custom metadata.\n"
        "\n"
        "All methods use a bidirectional stream. Each gRPC message in the two streams "
        "is one buffer.Buffer. The bee-rpc layer gives the meaning of these messages.\n"
        "\n"
        "A status that is not OK tells that the server refused the call or that the "
        "call failed. The status details are text for a person. A program must not "
        "parse them."
    )
    return ("grpc",), prose, formal


# ---------------------------------------------------------------------------------
# Layer 4: bee-rpc
# ---------------------------------------------------------------------------------

def bee_rpc_component() -> Layer:
    """``bee-rpc``: how a stream of ``buffer.Buffer`` carries objects of any size.

    Only what a receiver needs to read the stream is here. What it accepts in any
    variant is the sender's choice and therefore not protocol: the size of a chunk,
    whether the head shares a Buffer with the first chunk, whether a large item is sent
    as a block or inline, whether ``signal`` and ``skip`` are used at all. Nor is the
    library that implements it, nor how a receiver stores what it receives.
    """
    pairs = {
        "object": "concat(chunk) from first buffer to separator=true",
        "objects_per_stream": "many",
        "head.index": "selects the type of the object it starts; per method in celaut-gateway",
        "head.index.0": "raw bytes, never a message",
        "headless_object": "index 1, or index 0 when the method has no index 1",
        "head.partitions": "unused: senders do not set it, receivers ignore it",
        "empty_object": "absent, except buffer.Empty",
        "signal": "each signal=true toggles: first pauses the peer's sending, next resumes it",
        "signal_only_buffer": "carries no object",
        "block.start": "Buffer.block naming the block",
        "block.content": "the block's bytes, flat, as chunks",
        "block.end": "Buffer.block with the same hashes; always sent",
        "block.overlap": "forbidden",
        "block.id": "hash of the block's expanded bytes",
        "block.hash.type": "the algorithm's digest of the empty input",
        "block.hash.type_on_wire": "always present on every hash",
        "block.hash.required_type": Enviroment.hash_type.hex(),
        "block.previous_lengths_position": "offsets, in the expanded object, of the length varints that enclose the block",
        "block.object": "chunks and block contents concatenated in order are the serialized object",
        "skip": "receiver to sender: Buffer.skip names a block the receiver holds",
        "skip.sender": "may stop the block's content; still sends block.end",
        "skip.carrier": "Buffer{skip, chunk=empty}",
    }
    pairs.update(schema_pairs(buffer_pb2.DESCRIPTOR))
    formal = component_formal(pairs)

    prose = (
        "This layer is bee-rpc. It carries objects of any size in a stream of "
        "buffer.Buffer messages. The formal field gives the schema of buffer.Buffer.\n"
        "\n"
        "OBJECTS. One stream carries one or more objects. An object starts at a Buffer "
        "and stops at the first Buffer with separator set to true. The bytes of the "
        "object are the chunk fields of these Buffers, in sequence. A receiver must "
        "accept a chunk of any size. The first Buffer of an object can contain a head. "
        "The index of the head tells the type of the object. The celaut-gateway layer "
        "gives the types of each method. Index 0 is always raw bytes. If an object has "
        "no head, its index is 1. If the method has no index 1, its index is 0. An object "
        "with no bytes is not an object. The receiver ignores it, but buffer.Empty is "
        "an exception. Thus, if you send a message with all fields at their default "
        "values, the receiver does not get it.\n"
        "\n"
        "SIGNAL. A Buffer with signal set to true tells the other side to stop its "
        "stream. The next such Buffer tells it to continue. A Buffer that contains only "
        "a signal is not part of an object.\n"
        "\n"
        "BLOCKS. A sender can send a part of an object as a block. A Buffer with a "
        "block field starts the block. Then the bytes of the block follow as chunks. "
        "Then a Buffer with the same block field stops the block. The sender always "
        "sends this last Buffer. Two blocks must not overlap. The object is all the "
        "chunks and all the block bytes, in sequence. Each hash in a block field gives "
        "its type. The type of a hash algorithm is the digest of an empty input with "
        "that algorithm. The identifier of a block is the hash of its bytes. The formal "
        "field gives the hash type that the receiver uses to find blocks. Each block "
        "field must contain a hash of this type. The field "
        "previous_lengths_position gives the positions of the length varints that "
        "contain the block, in the bytes of the object.\n"
        "\n"
        "SKIP. A receiver that already has a block can tell this to the sender. It "
        "sends a Buffer with the skip field and an empty chunk, in the opposite "
        "direction. The skip field gives the hash of the block. The sender can then "
        "stop the bytes of that block. The sender must still send the Buffer that "
        "stops the block.\n"
        "\n"
        "The sender selects the size of each chunk. The sender also selects if it uses "
        "blocks, signal or skip. These are not part of the protocol. Senders do not set "
        "the partitions field of the head, and receivers ignore it."
    )
    return ("bee-rpc",), prose, formal


# ---------------------------------------------------------------------------------
# Layer 5: celaut-gateway
# ---------------------------------------------------------------------------------

def _type_name(message_type) -> str:
    return "bytes" if message_type is bytes else message_type.DESCRIPTOR.full_name


def _indices(table: Dict[int, type]) -> str:
    return ",".join(f"{i}={_type_name(t)}" for i, t in sorted(table.items()))


def _rpc_pairs() -> Dict[str, str]:
    """``rpc.<Method>`` -> its request and response indices and how it authenticates.

    From ``GATEWAY_RPCS``, which is what the server and the client read too. Every RPC
    the compiled service has must be in it, and nothing else may be: a method declared
    without its messages, or messages declared for a method nobody serves, is a
    declaration that cannot be implemented from.
    """
    served = {m.name for m in celaut_pb2.DESCRIPTOR.services_by_name[GATEWAY_SERVICE_NAME].methods}
    if served != set(GATEWAY_RPCS):
        raise RuntimeError(
            "protos.gateway_bee.GATEWAY_RPCS does not list the RPCs Gateway serves: "
            f"missing {sorted(served - set(GATEWAY_RPCS))}, "
            f"extra {sorted(set(GATEWAY_RPCS) - served)}."
        )
    return {
        f"rpc.{name}": f"in:{_indices(rpc.input)};out:{_indices(rpc.output)};auth:{rpc.auth}"
        for name, rpc in GATEWAY_RPCS.items()
    }


def _service_hash_types() -> str:
    """Every hash type this node's code understands for a service id, sorted.

    Not the one ``hashing.HASH`` selects: that is the operator's choice of which digest
    the registry keys services by (docs/CONFIG.md), so two nodes on the same code could
    differ on it, and nothing each node chooses for itself belongs in ``formal``. It is
    not something a caller has to agree on either -- a service id travels as
    ``Metadata.HashTag.Hash`` entries that each name their type, and the receiver picks
    the one it keys by. What both sides must share is the set of types either may send
    or read, which the code defines (``hashing.HASH_SPECS``).
    """
    from src.utils.hashing import HASH_SPECS
    return ",".join(sorted(hash_id.hex() for hash_id in HASH_SPECS))


def _indices_prose(table: Dict[int, type]) -> str:
    return ", ".join(f"index {i} {_type_name(t)}" for i, t in sorted(table.items())) or "nothing"


def _method_prose(name: str) -> str:
    """One RPC for the prose: its indices and auth kind from ``GATEWAY_RPCS``, then the
    description from its comment in ``celaut.proto`` (``protocol_doc``)."""
    rpc = GATEWAY_RPCS[name]
    description = protocol_doc.method_prose(GATEWAY_SERVICE_NAME, name) or ""
    return (
        f"{name}. Request: {_indices_prose(rpc.input)}. "
        f"Response: {_indices_prose(rpc.output)}. Authentication: {rpc.auth}. "
        f"{description}"
    ).rstrip()


def gateway_component() -> Layer:
    """``celaut-gateway``: the methods, their messages and every agreed format."""
    peer_id, ts, digest, client_id = (
        "<public_key_hex>", "<ts>", "<content_digest>", "<client_id>"
    )
    pairs = {
        "service": celaut_pb2.DESCRIPTOR.services_by_name[GATEWAY_SERVICE_NAME].full_name,
        "auth.kinds": ",".join(AUTH_KINDS),
        "auth.client": "Client at index 2 beside the payload at 1, any order; "
                       "a local instance of the node needs none",
        "client_id": "32 lowercase hex characters",
        "amount": "Amount.n: signed decimal integer string, in the MU of the receiving "
                  "node",
        "amount.negative": "ModifyDepositInput.difference: a withdrawal from the instance",
        "keyvalue": "producer: sorted by key (UTF-8 bytes), one entry per key, no empty "
                    "key; reader: last entry wins",
        "hash.type": "the algorithm's digest of the empty input",
        "service_id": "Metadata.HashTag.Hash entries, each with its hash.type; the "
                      "receiver reads the one type it keys its registry by",
        "service_id.hash_types": _service_hash_types(),
        "peer.ts": "unix seconds; accepted only if greater than the last accepted ts "
                   "for that public key",
        "peer.uri.expiry": "unix seconds; 0 means no estimate",
        "peer.signature": "Peer.signature_scheme over UTF-8 of payload, lowercase hex",
        "peer.signature.payload": canonical_peer_payload(peer_id, ts, digest),
        "peer.digest": "blake2b-256 hex of UTF-8 of "
                       "uris/contracts/proofs|rates|scheme|resources",
        "peer.digest.uris": "'/'.join(sorted(uri))",
        "peer.digest.uri": "ip~port~expiry~protocol(transport)~';'.join(sorted(protocol(stack)))",
        "peer.digest.protocol": "hex(formal)~','.join(sorted(tags))~prose",
        "peer.digest.contracts": "'/'.join(sorted(contract_message~mu_per_unit.n))",
        "peer.digest.proofs": "'/'.join(sorted(contract_message))",
        "peer.digest.contract_message": "hex(ledger.formal)~','.join(sorted(ledger.tags))"
                                        "~ledger.prose~';'.join(key=hex(value) by key)",
        "peer.digest.rates": "';'.join(key=amount.n by key)",
        "peer.digest.scheme": "';'.join(sorted(protocol(component)))",
        "peer.digest.resources": "'/'.join(sorted(protocol(architecture)~sysresources or '-'))",
        "peer.digest.sysresources": "','.join(name=value or name= for blkio_weight,"
                                    "cpu_period,cpu_quota,mem_limit,disk_space)"
                                    "~';'.join(sorted(key=value or key=))",
        "client_binding.payload": client_binding_payload(peer_id, client_id),
        "client_binding.signature": "Peer.signature_scheme, lowercase hex",
        "ledger_attestation.payload": attestation_payload(peer_id),
        "ledger_attestation.signature": "the scheme of the proof's ledger; "
                                        "xattrs owner_public_key, owner_signature",
        "pow.solution": f"blake2b-{hashlib.blake2b().digest_size * 8} hex of "
                        "UTF-8(challenge)||UTF-8(solution) ends with difficulty '0'",
        "pow.challenge": "opaque; the caller sends it back unchanged",
        "pow.retry": "same client_id",
        "introduce_peer.refused": "REFUSED",
        "recursion_guard.token": "opaque; the caller sends it again on a delegation",
        "tunnel.slot": "decimal internal port",
        "tunnel.end": "end of the caller's stream half-closes the service connection",
    }
    pairs.update(_rpc_pairs())
    pairs.update(schema_pairs(celaut_pb2.DESCRIPTOR))
    formal = component_formal(pairs)

    methods = "\n".join(_method_prose(name) for name in sorted(GATEWAY_RPCS))
    prose = (
        "This layer is the celaut gateway. The service is celaut.Gateway. The formal "
        "field gives the schema of all messages. It also gives, for each method, the "
        "type of each index in the two directions.\n"
        "\n"
        "AUTHENTICATION. Most methods need a Client. The caller sends the payload at "
        "index 1 and the Client at index 2, in any sequence. To get a client_id, call "
        "GenerateClient. A local instance of the node does not need a Client. Some "
        "methods use a token in the payload. ModifyServiceSystemResources uses the "
        "address of the caller. The formal field gives the kind for each method.\n"
        "\n"
        "FORMATS. A client_id is 32 lowercase hexadecimal characters. An Amount is a "
        "decimal integer with an optional minus sign, in the monetary unit of the node "
        "that receives it. A negative difference in ModifyDepositInput removes balance "
        "from the instance. A list "
        "of key-value entries is sorted by key, with one entry for each key. If a "
        "key occurs two times, the last entry is correct. The type of a hash "
        "algorithm is the digest of an empty input with that algorithm. The formal "
        "field gives the hash types that a node can use to identify a service. Each "
        "node selects one of them for its registry. Send a hash of each type that "
        "you have, each with its type.\n"
        "\n"
        "PEER SIGNATURE. The node signs its Peer with the scheme of "
        "Peer.signature_scheme. The signed text is the public key, the ts and a "
        "content digest, with a vertical bar between them. The content digest is a "
        "BLAKE2b-256 hash of a text that the formal field specifies. This text "
        "contains all the fields of the Peer, in a sorted sequence. The ts is in unix "
        "seconds. Accept a Peer only if its ts is more than the last ts that you "
        "accepted for the same public key.\n"
        "\n"
        "OTHER SIGNATURES. To bind a client_id to its identity, a peer signs the "
        "text \"" + client_binding_payload("<peer_id>", "<client_id>") + "\". The wallet "
        "of a reputation proof signs the text \"" + attestation_payload("<peer_id>") +
        "\". The prefixes keep a signature for one use from a different use.\n"
        "\n"
        "PROOF OF WORK. GenerateClient can return a PoWRequired. Find a solution text. "
        "Calculate the BLAKE2b hash, with a 64-byte digest, of the challenge followed "
        "by the solution, both in UTF-8. The hexadecimal digest must end with a number "
        "of zeros equal to the difficulty. Send the challenge again without a change.\n"
        "\n"
        "METHODS. Each method gives the type of each index in the request and in the "
        "response, and its kind of authentication.\n" + methods + "\n"
        "\n"
        "MESSAGES. Each message gives its full name and what it is. Then each field "
        "gives its number, its name, its cardinality, its type and what it is. The "
        "formal field identifies a field only by its number. The names are only for "
        "a reader.\n"
        "\n" + protocol_doc.messages_prose()
    )
    return ("celaut-gateway",), prose, formal


# ---------------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------------

def _layers() -> Tuple[Layer, ...]:
    return (
        tls_component(),
        http2_component(),
        grpc_component(),
        bee_rpc_component(),
        gateway_component(),
    )


def declare_transport(uri, *, prose: bool = True) -> None:
    """Declare ``uri.transport``, replacing whatever was there."""
    tags, description, formal = transport_component()
    uri.transport.CopyFrom(celaut_pb2.Peer.Uri.Protocol(
        tags=list(tags), prose=description if prose else "", formal=formal
    ))


def declare_transport_stack(uri, *, prose: bool = True) -> None:
    """Declare on ``uri`` the stack its address speaks, replacing anything already there.

    ``prose=False`` drops the descriptions, for the same reason
    ``node_identity.declare_signature_scheme`` does: they are what lets a reader learn
    the protocol, never what decides a comparison, and they cost bytes on every
    ``GetPeerInfo``.
    """
    del uri.protocol_stack[:]
    for tags, description, formal in _layers():
        uri.protocol_stack.add(
            tags=list(tags), prose=description if prose else "", formal=formal
        )


def node_transport_stack(*, prose: bool = True):
    """This node's stack as a detached list of ``Peer.Uri.Protocol`` messages."""
    uri = celaut_pb2.Peer.Uri()
    declare_transport_stack(uri, prose=prose)
    return list(uri.protocol_stack)


def node_transport():
    """This node's ``Peer.Uri.transport`` as a detached ``Peer.Uri.Protocol``."""
    uri = celaut_pb2.Peer.Uri()
    declare_transport(uri)
    return uri.transport


def share_prose_on_get_peer_info() -> bool:
    """Whether an announcement served over gRPC carries its prose. Off by default.

    ``GetPeerInfo`` answers every caller, so the descriptions are paid per address on
    every call, to callers the node knows nothing about. Nothing that decides a
    comparison is in them -- ``formal`` and the tags are what a reader matches on -- so
    the default buys back the bandwidth and keeps the answer. Turn it on to serve an
    announcement complete enough to implement the protocol from, which is the one thing
    holding the prose back costs (``nodo protocol`` shows it either way).
    """
    return bool(ConfigManager().get(SHARE_PROSE_ON_GET_PEER_INFO_KEY, False))


def share_prose_on_ledger() -> bool:
    """Whether an announcement written to a ledger carries its prose. Off by default.

    A reputation box pays storage rent on every byte for as long as it exists, and these
    paragraphs are several times a register's whole budget.
    """
    return bool(ConfigManager().get(SHARE_PROSE_ON_LEDGER_KEY, False))


def carries_prose(peer) -> bool:
    """Whether ``peer`` declares any prose at all.

    Asked before stripping one, because stripping is only free on a message this node is
    about to sign: on someone else's it costs their signature, so a peer that announced
    none must not be treated as if it had.
    """
    if any(c.prose for c in peer.signature_scheme.components):
        return True
    return any(
        c.prose for uri in peer.uri
        for c in list(uri.protocol_stack) + [uri.transport]
    )


def compatible_layers(a, b) -> bool:
    """Whether two layers at the same position let two nodes talk.

    Both sides must carry a non-empty ``formal``: a layer that declares only tags names
    a protocol without its parameters, so it gives this node nothing to check. Equal
    bytes are compatible. Other bytes are compatible only when :func:`formal_conflicts`
    finds no key on which the two contradict each other.
    """
    formal_a, formal_b = bytes(a.formal), bytes(b.formal)
    if not formal_a or not formal_b:
        return False
    return formal_a == formal_b or not formal_conflicts(formal_a, formal_b)


def compatible_layer_stacks(a_layers, b_layers) -> bool:
    """Whether two ordered stacks of layers let two nodes talk.

    Layer ``i`` against layer ``i``, with :func:`compatible_layers` for each pair, and
    the same number of layers on both sides. Unlike a signature scheme, whose building
    blocks have no order, a stack does: ``grpc`` over ``tls`` is not ``tls`` over
    ``grpc``, and reading it in order is also what keeps the comparison linear in a
    length a peer chooses.
    """
    a_layers, b_layers = list(a_layers), list(b_layers)
    if len(a_layers) != len(b_layers):
        return False
    return all(compatible_layers(a, b) for a, b in zip(a_layers, b_layers))


def speaks_our_transport_stack(protocol_stack: Iterable) -> bool:
    """Whether an announced stack is the one this node speaks.

    Compared in order (:func:`compatible_layer_stacks`). So a peer whose TLS extension
    OID, signed payload, field types or RPC messages differ from this node's is seen as
    speaking something else. A peer that only worded its prose differently, or that
    has a message field or an RPC more or less than this node, is not.

    An empty stack is refused like any other: an address that declares nothing gives a
    caller nothing to check before dialling it, and every node declares its stack.
    """
    return compatible_layer_stacks(list(protocol_stack), node_transport_stack(prose=False))


def _formal_pairs(formal: bytes) -> Dict[str, str]:
    """``formal`` as its ``key=value`` pairs, or as one opaque entry when it is not that.

    A peer's ``formal`` is whatever it chose to write, and a comparison report has to
    show it either way: one that does not parse is still a declaration, only not one
    that can be diffed key by key.
    """
    try:
        return parse_component_formal(formal)
    except ComponentFormalError:
        return {"<formal>": bytes(formal).hex()}


def formal_difference(ours: bytes, theirs: bytes) -> Dict[str, Dict[str, Optional[str]]]:
    """The keys on which two ``formal`` fields disagree, with each side's value.

    ``None`` stands for a key one side does not declare. Only meaningful between two
    components already paired as the same layer: across two different layers every key
    differs, and saying so is no information.
    """
    a, b = _formal_pairs(ours), _formal_pairs(theirs)
    return {
        key: {"ours": a.get(key), "theirs": b.get(key)}
        for key in sorted(set(a) | set(b))
        if a.get(key) != b.get(key)
    }


# Keys whose values are items of a set that protobuf and gRPC let grow: a message of the
# schema, and an RPC. One side can declare an item the other does not.
_EXTENSIBLE_KEY_PREFIXES: Final = ("schema.", "rpc.")


def _schema_fields(value: str) -> Optional[Dict[str, str]]:
    """A ``schema.<message>`` value as ``{field number: "cardinality:type"}``.

    None when the value does not have the form ``protocol_schema`` writes.
    """
    fields: Dict[str, str] = {}
    for item in value.split(",") if value else ():
        number, sep, rest = item.partition(":")
        if not sep or not number.isdigit() or number in fields:
            return None
        fields[number] = rest
    return fields


def _schemas_agree(a: str, b: str) -> bool:
    """Whether two declarations of one message agree on every field both declare.

    A field number that only one side declares is not a contradiction: the protobuf
    reader on the other side ignores it, or reads its default value.
    """
    fields_a, fields_b = _schema_fields(a), _schema_fields(b)
    if fields_a is None or fields_b is None:
        return False
    return all(fields_a[n] == fields_b[n] for n in set(fields_a) & set(fields_b))


def formal_conflicts(ours: bytes, theirs: bytes) -> Dict[str, Dict[str, Optional[str]]]:
    """The keys on which two ``formal`` fields contradict each other.

    The rules:

    - A ``schema.<message>`` key that both sides declare must agree on each field
      number that both sides declare (the same cardinality and the same type).
    - An ``rpc.<Method>`` key that both sides declare must be equal: the bee-rpc
      indices and the auth kind are what both ends of the call read.
    - A ``schema.*`` or ``rpc.*`` key that only one side declares is not a conflict.
    - Every other key must be on both sides, with the same value.

    The same form as :func:`formal_difference`, with ``None`` for a key one side does
    not declare. An empty result means the two layers are compatible.
    """
    a, b = _formal_pairs(ours), _formal_pairs(theirs)
    conflicts: Dict[str, Dict[str, Optional[str]]] = {}
    for key in sorted(set(a) | set(b)):
        if a.get(key) == b.get(key):
            continue
        if key.startswith(_EXTENSIBLE_KEY_PREFIXES):
            if key not in a or key not in b:
                continue
            if key.startswith("schema.") and _schemas_agree(a[key], b[key]):
                continue
        conflicts[key] = {"ours": a.get(key), "theirs": b.get(key)}
    return conflicts


def compare_component_sets(ours: Iterable, theirs: Iterable) -> List[Dict]:
    """:func:`compare_layer_stacks` for components that have no order.

    A signature scheme is an unordered set of building blocks
    (``node_identity.same_signature_scheme``), so its report pairs components rather
    than positions: exact matches first, then components sharing a tag (``differs``),
    then what is left on each side (``missing`` / ``extra``). Greedy, so on components
    whose tags overlap it can pair differently from the exhaustive search behind the
    verdict -- which is why a caller reports that verdict, and this as its explanation.
    """
    ours, theirs = list(ours), list(theirs)
    unpaired = list(range(len(theirs)))
    pairs: Dict[int, Tuple[int, str]] = {}

    for status, pairs_with in (
        ("match", same_component),
        ("differs", lambda a, b: bool(set(a.tags) & set(b.tags))),
    ):
        for i, component in enumerate(ours):
            if i in pairs:
                continue
            j = next((j for j in unpaired if pairs_with(component, theirs[j])), None)
            if j is not None:
                pairs[i] = (j, status)
                unpaired.remove(j)

    report: List[Dict] = []
    for i, component in enumerate(ours):
        entry = {"tags": list(component.tags)}
        if i not in pairs:
            entry["status"] = "missing"
        else:
            j, status = pairs[i]
            entry["status"] = status
            entry["their_tags"] = list(theirs[j].tags)
            if status == "differs":
                entry["formal_difference"] = formal_difference(
                    bytes(component.formal), bytes(theirs[j].formal)
                )
        report.append(entry)
    for j in unpaired:
        report.append({"tags": list(theirs[j].tags), "status": "extra"})
    return report


def compare_layer_stacks(ours: Iterable, theirs: Iterable) -> List[Dict]:
    """Layer by layer, where two ordered stacks agree and where not.

    The explanation of :func:`compatible_layer_stacks`'s verdict, for whoever has to act on it
    -- an operator whose peer was skipped, a developer whose change altered what the
    node announces. One entry per position, each with a ``status``:

    ``match``
        The two layers at that position declare the same ``formal``, byte for byte.
    ``compatible``
        The two layers differ, but only by ``schema.*`` or ``rpc.*`` items that one
        side declares and the other does not (:func:`formal_conflicts`).
        ``formal_difference`` names them.
    ``differs``
        Both sides have a layer there and they are not compatible.
        ``formal_difference`` names the conflicting keys when the two share a tag --
        the same protocol with other parameters -- and is left out when they name
        different protocols.
    ``missing``
        A layer of ours with nothing at that position in theirs.
    ``extra``
        A layer of theirs with nothing at that position in ours.
    """
    ours, theirs = list(ours), list(theirs)
    layers: List[Dict] = []
    for position in range(max(len(ours), len(theirs))):
        if position >= len(theirs):
            layers.append({"tags": list(ours[position].tags), "status": "missing"})
            continue
        if position >= len(ours):
            layers.append({"tags": list(theirs[position].tags), "status": "extra"})
            continue
        a, b = ours[position], theirs[position]
        entry = {"tags": list(a.tags), "their_tags": list(b.tags)}
        formal_a, formal_b = bytes(a.formal), bytes(b.formal)
        if formal_a and formal_a == formal_b:
            entry["status"] = "match"
        elif compatible_layers(a, b):
            entry["status"] = "compatible"
            entry["formal_difference"] = formal_difference(formal_a, formal_b)
        else:
            entry["status"] = "differs"
            if set(a.tags) & set(b.tags):
                entry["formal_difference"] = formal_conflicts(formal_a, formal_b)
        layers.append(entry)
    return layers
