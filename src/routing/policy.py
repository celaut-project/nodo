"""What the operator decided about routing: ``routing.HIDDEN`` and ``routing.tor.*``."""

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
