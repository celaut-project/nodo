"""Name resolution through Tor, for a hidden node.

The host's own resolver must not look up a name that a service spec or a peer gives.
Whoever selects the name also controls its authoritative DNS server, and that server
sees the resolver that asks: the address of the node with a local recursive resolver,
the /24 of the node with EDNS Client Subnet, at least the ISP and the time of the
launch otherwise. So in hidden mode such a name goes to Tor's ``DNSPort`` on loopback
(``TorBackend.dns_port``), and an exit resolves it.

Only A records: guests get IPv4 only. This is a minimal client on purpose (one query,
one UDP datagram, no recursion of its own), because Tor's ``DNSPort`` does the work and
nodo has no DNS library.

The seeds of the operator (``service_networks.default_instances``) are not sent here: the
operator chose them, and a name on the operator's LAN cannot resolve at an exit. The PoW
check sends a name of the operator here first, and asks the host's resolver only when the
exit says that the name does not exist (``src/manager/pow_networks.py``).
"""

import ipaddress
import os
import socket
import struct
from typing import List

from src.routing.base import RoutingUnavailable

TIMEOUT_S = 15.0
_TYPE_A = 1
_CLASS_IN = 1
_RCODE_NXDOMAIN = 3


class NameNotFound(ValueError):
    """The exit says that the name does not exist (NXDOMAIN).

    Not a timeout and not a transient failure: only this answer tells that the name is
    not on the public DNS, for example a name on the LAN of the operator.
    """


def resolve_ipv4(name: str, timeout: float = TIMEOUT_S) -> List[str]:
    """The IPv4 addresses of ``name``, resolved at a Tor exit.

    An address literal is returned as it is, without a query. Raises
    ``RoutingUnavailable`` while Tor is not ready, and ``ValueError`` when the name
    does not resolve. Never falls back to the host's resolver.
    """
    text = str(name or "").strip().strip("[]").rstrip(".")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        return [str(address)] if address.version == 4 else []

    from src.routing import registry

    port = registry.TOR.dns_port()
    if port is None:
        raise RoutingUnavailable(f"hidden mode: Tor is not ready to resolve {text!r}.")
    return query_a(text, ("127.0.0.1", port), timeout=timeout)


def query_a(name: str, server, timeout: float = TIMEOUT_S) -> List[str]:
    """Send one A query for ``name`` to ``server`` (ip, port) over UDP. The addresses."""
    query_id = struct.unpack("!H", os.urandom(2))[0]
    packet = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    packet += _encode_name(name) + struct.pack("!HH", _TYPE_A, _CLASS_IN)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect(server)
        sock.send(packet)
        while True:
            try:
                reply = sock.recv(4096)
            except socket.timeout:
                raise ValueError(f"no DNS answer for {name!r} through Tor") from None
            if len(reply) >= 2 and struct.unpack("!H", reply[:2])[0] == query_id:
                return _parse_a_answers(reply, name)


def _encode_name(name: str) -> bytes:
    try:
        ascii_name = name.encode("idna")
    except UnicodeError as e:
        raise ValueError(f"{name!r} is not a valid host name: {e}") from e
    labels = ascii_name.split(b".")
    if not ascii_name or len(ascii_name) > 253 or any(not 0 < len(label) < 64 for label in labels):
        raise ValueError(f"{name!r} is not a valid host name.")
    return b"".join(bytes([len(label)]) + label for label in labels) + b"\x00"


def _skip_name(data: bytes, offset: int) -> int:
    """The offset after a (possibly compressed) name that starts at ``offset``."""
    while True:
        if offset >= len(data):
            raise ValueError("truncated DNS answer")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += 1 + length


def _parse_a_answers(data: bytes, name: str) -> List[str]:
    if len(data) < 12:
        raise ValueError("truncated DNS answer")
    _, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", data[:12])
    rcode = flags & 0x000F
    if rcode == _RCODE_NXDOMAIN:
        raise NameNotFound(f"{name!r} does not exist (through Tor)")
    if rcode != 0:
        raise ValueError(f"{name!r} did not resolve through Tor (DNS rcode {rcode})")
    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(data, offset) + 4
    addresses: List[str] = []
    for _ in range(ancount):
        offset = _skip_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("truncated DNS answer")
        rtype, rclass, _, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + length]
        offset += length
        if rtype == _TYPE_A and rclass == _CLASS_IN and len(rdata) == 4:
            addresses.append(socket.inet_ntoa(rdata))
    if not addresses:
        raise ValueError(f"{name!r} has no IPv4 address (through Tor)")
    return addresses
