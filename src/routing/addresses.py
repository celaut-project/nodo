"""Which addresses a hidden node reaches directly, as pure functions.

No config import, on purpose: ``config_validation`` checks ``routing.LOCAL_NETWORKS``
with :func:`parse_local_networks` while ``ConfigManager`` loads, and ``routing.policy``
reads the same key after it.
"""

import ipaddress
from typing import Iterable, Tuple, Union

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# The ranges that stay on the networks of this host: loopback, the private ranges of
# RFC 1918, link-local, and the IPv6 equivalents. A fixed list, not ``is_global`` or
# ``is_private``: those two give a different answer for the same address in different
# Python versions, and they count as local some ranges that are not -- the shared address
# space 100.64.0.0/10 (the network of the carrier, on an ISP with CGNAT), the benchmark
# range 198.18.0.0/15, 192.0.0.0/24, 0.0.0.0/8 and the documentation ranges. A node
# that sent those out directly would show its own address (#550, P4).
BUILT_IN_LOCAL_NETWORKS: Tuple[Network, ...] = tuple(ipaddress.ip_network(text) for text in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
))

# Where a range in ``routing.LOCAL_NETWORKS`` must be: a range that is not on the public
# Internet. The built-in local ranges, plus the shared address space (CGNAT, and the
# addresses Tailscale gives) and the benchmark range, which can be the operator's
# own network but are not always. A public range is refused: it would send traffic to
# the Internet directly, from the address of the node.
DECLARABLE_PARENTS: Tuple[Network, ...] = BUILT_IN_LOCAL_NETWORKS + tuple(
    ipaddress.ip_network(text) for text in ("100.64.0.0/10", "198.18.0.0/15")
)


def parse_local_networks(values) -> Tuple[Network, ...]:
    """``routing.LOCAL_NETWORKS`` as networks. Raises ``ValueError`` with the reason.

    A list of CIDRs (``100.64.0.0/10``, ``100.101.102.0/24``) or bare addresses. Each
    one must be inside a range that is not on the public Internet
    (:data:`DECLARABLE_PARENTS`).
    """
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise ValueError("routing.LOCAL_NETWORKS must be a list of CIDRs.")
    parsed = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"routing.LOCAL_NETWORKS: {value!r} is not a CIDR.")
        try:
            network = ipaddress.ip_network(value.strip(), strict=False)
        except ValueError as e:
            raise ValueError(f"routing.LOCAL_NETWORKS: {value!r} is not a CIDR: {e}") from None
        if not any(
            network.version == parent.version and network.subnet_of(parent)
            for parent in DECLARABLE_PARENTS
        ):
            raise ValueError(
                f"routing.LOCAL_NETWORKS: {value!r} is not a private range. Only "
                "ranges that are not on the public Internet can be declared: "
                + ", ".join(str(parent) for parent in DECLARABLE_PARENTS) + "."
            )
        parsed.append(network)
    return tuple(parsed)


def is_local_address(host: str, extra: Iterable[Network] = ()) -> bool:
    """Whether ``host`` is in a built-in local range, or in one of ``extra``.

    ``localhost`` is local. A name that is not an address is not: it is not resolved here.
    """
    text = str(host or "").strip().strip("[]").lower()
    if text == "localhost":
        return True
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    # ::ffff:10.0.0.1 is the IPv4 address 10.0.0.1.
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(
        address in network
        for network in (*BUILT_IN_LOCAL_NETWORKS, *extra)
        if network.version == address.version
    )
