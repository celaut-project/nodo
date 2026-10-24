"""What the operator decided about routing: ``routing.HIDDEN`` and ``routing.tor.*``."""

from src.routing import addresses
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


def ledger_via_tor() -> bool:
    """Whether the node's own requests to its ledger services go through Tor.

    ``routing.tor.LEDGER_VIA_TOR``: ``true`` and ``false`` are literal. ``auto`` (the
    default) is Tor in hidden mode and direct otherwise. These requests (the Ergo
    explorer and node, the Bitcoin explorer and backend, the reputation reads, the energy
    meter) go to services that the operator chose, but the service still sees the IP
    address of the host. An address of the operator's own networks is never sent
    through Tor (see ``is_local_address``).
    """
    value = env_manager.get("routing.tor.LEDGER_VIA_TOR", "auto")
    if isinstance(value, bool):
        return value
    return hidden_mode()


LOCAL_NETWORKS_KEY = "routing.LOCAL_NETWORKS"


def local_networks() -> tuple:
    """``routing.LOCAL_NETWORKS``: the ranges the operator says are its own networks.

    Validated when the config loads (``config_validation.validate_routing_config``). A
    value that fails here anyway gives no range, never a wider one.
    """
    try:
        return addresses.parse_local_networks(env_manager.get(LOCAL_NETWORKS_KEY, []))
    except ValueError:
        return ()


def is_local_address(host: str) -> bool:
    """Whether ``host`` is an address that does not leave this host's own networks.

    Loopback, private and link-local addresses, ``localhost``, and the ranges in
    ``routing.LOCAL_NETWORKS``. Traffic to them shows the node's address only to the
    operator's own networks, never to a peer, so hidden mode does not send it through
    Tor (an exit could not reach it anyway). Everything else is not local, including
    the shared address space 100.64.0.0/10 unless the operator declares it
    (``src/routing/addresses.py``).
    """
    return addresses.is_local_address(host, extra=local_networks())
