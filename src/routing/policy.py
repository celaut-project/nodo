"""What the operator decided about routing: ``routing.HIDDEN`` and ``routing.tor.*``."""

import ipaddress

from src.utils.config import ConfigManager

env_manager = ConfigManager()


def hidden_mode() -> bool:
    """Whether this node hides its IP address from its peers.

    In hidden mode the node announces only onion addresses, listens on loopback only,
    and never calls a peer outside a routing backend.
    """
    return env_manager.get("routing.HIDDEN", False) is True


def tor_enabled_setting():
    """``routing.tor.ENABLED`` as written: ``True``, ``False`` or ``"auto"``."""
    value = env_manager.get("routing.tor.ENABLED", "auto")
    return value if isinstance(value, bool) else "auto"


def announce_onion() -> bool:
    """Whether this node announces an onion address for its gateway.

    ``routing.tor.ANNOUNCE``: ``true`` and ``false`` are literal. ``auto`` (the default)
    announces when Tor is turned on explicitly (``routing.tor.ENABLED: true``) and in
    hidden mode. With ``ENABLED: auto``, a node that happens to have Tor installed
    calls ``.onion`` peers and opens no listener.
    """
    value = env_manager.get("routing.tor.ANNOUNCE", "auto")
    if isinstance(value, bool):
        return value
    return hidden_mode() or tor_enabled_setting() is True


def clearnet_via_exit() -> bool:
    """Whether hidden mode may call clearnet peers through a Tor exit."""
    return env_manager.get("routing.tor.CLEARNET_VIA_EXIT", False) is True


def onion_virtual_port() -> int:
    """The port peers dial on the onion name: the gateway port, as on the clearnet."""
    return int(env_manager.get_gateway_port())


def service_egress_via_tor() -> bool:
    """Whether a hidden node sends the outside traffic of its services through Tor.

    ``routing.tor.SERVICE_EGRESS`` (default true), in hidden mode only. The TCP that a
    service sends outside this host goes to Tor's ``TransPort`` instead of out of the
    host's interface, so the other end sees a Tor exit. UDP has no such path.
    """
    if not hidden_mode():
        return False
    return env_manager.get("routing.tor.SERVICE_EGRESS", True) is not False


# The ranges that stay on the networks of this host: loopback, the private ranges of
# RFC 1918, link-local, and the IPv6 equivalents. A fixed list, not ``is_global`` or
# ``is_private``: those two give a different answer for the same address in different
# Python versions, and they count as local some ranges that are not -- the shared address
# space 100.64.0.0/10 (the network of the carrier, on an ISP with CGNAT), the benchmark
# range 198.18.0.0/15, 192.0.0.0/24, 0.0.0.0/8 and the documentation ranges. A node
# that sent those out directly would show its own address (#550, P4).
_LOCAL_NETWORKS = tuple(ipaddress.ip_network(text) for text in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
))


def is_local_address(host: str) -> bool:
    """Whether ``host`` is an address that does not leave this host's own networks.

    Loopback, private and link-local addresses, and ``localhost``. Traffic to them shows
    the node's address only to its own LAN, never to a peer, so hidden mode does not
    send it through Tor (an exit could not reach it anyway). Everything else is not
    local, including the shared address space 100.64.0.0/10.
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
    return any(address in network for network in _LOCAL_NETWORKS if network.version == address.version)
