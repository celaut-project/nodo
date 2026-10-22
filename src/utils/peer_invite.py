"""A compact peer invitation, small enough for a QR code (issue #572).

An invitation names a peer by its identity key and lists where to reach it. It carries
no signature, no resources and no proofs: a node that scans it connects, checks that the
remote end holds ``public_key``, and then fetches the full signed ``Peer`` with
``GetPeerInfo``. The address is never trusted, only the key.

Each ``Addr`` is described by tags, which are synonyms, and the tag defines the format of
its ``data`` (the host and then the port). Adding a kind of address is one entry in
``_CODECS``.
"""

import base64
import hashlib
import ipaddress
import struct
from typing import Callable, Dict, List, NamedTuple, Optional

from google.protobuf.message import DecodeError

from protos import invite_pb2
from src.identity.node_identity import ComponentFormalError, parse_component_formal

PUBLIC_KEY_LENGTH = 32
# A QR code holds 2953 bytes at level L and 2331 at level M. This keeps it scannable.
MAX_QR_BYTES = 1000

_PORT = struct.Struct(">H")
_ONION_VERSION = b"\x03"


class InviteError(ValueError):
    """An invitation, or one of its addresses, could not be built or read."""


class InviteTooLarge(InviteError):
    """The invitation does not fit in a QR code."""


class _Codec(NamedTuple):
    # "host:port" as ``nodo connect`` takes it, to the ``data`` bytes of the tag.
    encode: Callable[[str], bytes]
    decode: Callable[[bytes], str]


def _split_port(data: bytes, host_length: int, tag: str) -> bytes:
    if len(data) != host_length + _PORT.size:
        raise InviteError(
            f"The data of a {tag!r} address is {len(data)} bytes, "
            f"expected {host_length + _PORT.size}."
        )
    return data[:host_length]


def _port_of(text: str) -> int:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise InviteError(f"Not a host:port address: {text!r}.")
    return int(port)


def _host_of(text: str) -> str:
    host = text.rpartition(":")[0]
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host


def _ip_codec(version: int, tag: str) -> _Codec:
    length = 4 if version == 4 else 16

    def encode(text: str) -> bytes:
        try:
            host = ipaddress.ip_address(_host_of(text))
        except ValueError:
            raise InviteError(f"Not an IPv{version} address: {text!r}.") from None
        if host.version != version:
            raise InviteError(f"Not an IPv{version} address: {text!r}.")
        return host.packed + _PORT.pack(_port_of(text))

    def decode(data: bytes) -> str:
        host = ipaddress.ip_address(_split_port(data, length, tag))
        port = _PORT.unpack(data[length:])[0]
        return f"{host}:{port}" if version == 4 else f"[{host}]:{port}"

    return _Codec(encode, decode)


def _onion_text(key: bytes) -> str:
    checksum = hashlib.sha3_256(b".onion checksum" + key + _ONION_VERSION).digest()[:2]
    return base64.b32encode(key + checksum + _ONION_VERSION).decode().lower() + ".onion"


def _encode_tor3(text: str) -> bytes:
    host = _host_of(text)
    try:
        raw = base64.b32decode(host.removesuffix(".onion").upper())
    except ValueError:
        raise InviteError(f"Not a v3 onion address: {text!r}.") from None
    key = raw[:PUBLIC_KEY_LENGTH]
    if not host.endswith(".onion") or len(raw) != 35 or _onion_text(key) != host.lower():
        raise InviteError(f"Not a v3 onion address: {text!r}.")
    return key + _PORT.pack(_port_of(text))


def _decode_tor3(data: bytes) -> str:
    key = _split_port(data, PUBLIC_KEY_LENGTH, "tor3")
    return f"{_onion_text(key)}:{_PORT.unpack(data[PUBLIC_KEY_LENGTH:])[0]}"


_CODECS: Dict[str, _Codec] = {
    "ip4": _ip_codec(4, "ip4"),
    "ip6": _ip_codec(6, "ip6"),
    "tor3": _Codec(_encode_tor3, _decode_tor3),
}


def address_to_data(tag: str, text: str) -> bytes:
    """The ``data`` of an ``Addr`` of kind ``tag`` that points at ``host:port`` ``text``."""
    if tag not in _CODECS:
        raise InviteError(f"Unknown address tag {tag!r}.")
    return _CODECS[tag].encode(text)


def data_to_address(tag: str, data: bytes) -> str:
    """The ``host:port`` that the ``data`` of an ``Addr`` of kind ``tag`` points at."""
    if tag not in _CODECS:
        raise InviteError(f"Unknown address tag {tag!r}.")
    return _CODECS[tag].decode(bytes(data))


def known_tag(addr: invite_pb2.Addr) -> Optional[str]:
    """The first tag of ``addr`` this node understands, or None to ignore the address."""
    return next((tag for tag in addr.tags if tag in _CODECS), None)


def _tag_of_host(host: str) -> Optional[str]:
    if host.lower().endswith(".onion"):
        return "tor3"
    try:
        return "ip4" if ipaddress.ip_address(host).version == 4 else "ip6"
    except ValueError:
        return None


def build_invite(peer) -> bytes:
    """The invitation to ``peer``, a ``celaut.Peer``, taking its key and its addresses.

    The addresses keep the order of the Peer, which puts the reachable ones first. One
    whose host has no tag (a host name) is left out: its ``data`` would need a format.
    """
    try:
        public_key = bytes.fromhex(peer.public_key)
    except ValueError:
        public_key = b""
    if len(public_key) != PUBLIC_KEY_LENGTH:
        raise InviteError("The peer has no identity key to invite with.")

    invite = invite_pb2.PeerInvite(public_key=public_key)
    for uri in peer.uri:
        tag = _tag_of_host(uri.ip)
        if tag is None:
            continue
        host = f"[{uri.ip}]" if tag == "ip6" else uri.ip
        invite.addr.add(tags=[tag], data=address_to_data(tag, f"{host}:{uri.port}"))
    return invite.SerializeToString(deterministic=True)


def parse_invite(raw: bytes) -> invite_pb2.PeerInvite:
    """The invitation in ``raw``, without the addresses this node does not know.

    An address of an unknown kind is dropped, not refused. One of a known kind whose
    data is malformed refuses the whole invitation, since it is not a valid one.
    """
    invite = invite_pb2.PeerInvite()
    try:
        invite.ParseFromString(bytes(raw))
    except DecodeError as e:
        raise InviteError(f"Not a peer invitation: {e}.") from None
    if len(invite.public_key) != PUBLIC_KEY_LENGTH:
        raise InviteError(
            f"The identity key is {len(invite.public_key)} bytes, expected {PUBLIC_KEY_LENGTH}."
        )

    known: List[invite_pb2.Addr] = []
    for addr in invite.addr:
        try:
            parse_component_formal(addr.formal)
        except ComponentFormalError as e:
            raise InviteError(f"An address has a malformed formal: {e}") from None
        tag = known_tag(addr)
        if tag is None:
            continue
        data_to_address(tag, addr.data)
        known.append(addr)
    del invite.addr[:]
    invite.addr.extend(known)
    return invite


def invite_targets(invite: invite_pb2.PeerInvite) -> List[str]:
    """The ``host:port`` of each address of a parsed ``invite`` this node understands."""
    return [data_to_address(known_tag(addr), addr.data) for addr in invite.addr if known_tag(addr)]


def peer_id_of(invite: invite_pb2.PeerInvite) -> str:
    """The peer_id the invitation is for: its public key in the lowercase hex of a Peer."""
    return invite.public_key.hex()


def qr_for_invite(raw: bytes):
    """The QR code of ``raw``, in byte mode at error correction level M.

    Raises :class:`InviteTooLarge`, naming the addresses to remove, over ``MAX_QR_BYTES``.
    """
    import segno

    if len(raw) > MAX_QR_BYTES:
        raise InviteTooLarge(_too_large_message(raw))
    return segno.make(raw, mode="byte", error="m", boost_error=False)


def _too_large_message(raw: bytes) -> str:
    invite = invite_pb2.PeerInvite()
    invite.ParseFromString(raw)
    keep = invite_pb2.PeerInvite(public_key=invite.public_key)
    for index, addr in enumerate(invite.addr):
        keep.addr.append(addr)
        if keep.ByteSize() > MAX_QR_BYTES:
            drop = ", ".join(
                f"#{i + 1} ({invite.addr[i].tags[0] if invite.addr[i].tags else '?'})"
                for i in range(index, len(invite.addr))
            )
            break
    else:
        drop = "the key and the addresses do not fit"
    return (
        f"The invitation is {len(raw)} bytes and a QR code takes at most {MAX_QR_BYTES}. "
        f"Remove the Addr entries {drop}."
    )
