"""Which backend reaches an address."""

import atexit
import threading
from typing import Iterable, Optional, Tuple

from src.routing.base import RoutingBackend, RoutingUnavailable
from src.routing.clearnet import ClearnetBackend
from src.routing.policy import clearnet_via_exit, hidden_mode
from src.routing.tor import TorBackend


class _BlockedBackend(RoutingBackend):
    """Stands in for the clearnet in hidden mode, where nothing may be dialled directly."""

    name = "blocked"
    tag = ""

    def handles_host(self, host: str) -> bool:
        return False

    def available(self) -> bool:
        return False

    def open_socket(self, host, port, timeout):
        raise RoutingUnavailable(
            f"{host}:{port} is a clearnet address, and this node is in hidden mode "
            "(routing.HIDDEN). Set routing.tor.CLEARNET_VIA_EXIT to call it through a Tor exit."
        )

    def grpc_channel_options(self, host):
        raise RoutingUnavailable(f"{host} is a clearnet address, and this node is in hidden mode.")


CLEARNET = ClearnetBackend()
TOR = TorBackend()
BLOCKED = _BlockedBackend()

#: Every backend this build knows, clearnet first.
BACKENDS = (CLEARNET, TOR)

#: Tags of the clearnet transports. A routed tag must never share a transport with one.
CLEARNET_TAGS = frozenset({"tcp", "udp"})


def backend_for_tag(tag: str) -> Optional[RoutingBackend]:
    tag = str(tag or "").strip().lower()
    return next((b for b in BACKENDS if b.tag == tag), None)


def routed_tags() -> frozenset:
    """Tags of the backends that are not the clearnet."""
    return frozenset(b.tag for b in BACKENDS if b is not CLEARNET)


def routed_host(host: str) -> bool:
    """Whether ``host`` is a name that only a routing backend can reach (``.onion``)."""
    return any(b.handles_host(host) for b in BACKENDS)


def backend_for_host(host: str) -> RoutingBackend:
    """The backend ``host`` has to go through. A ``.onion`` name always goes to Tor.

    Chosen by the name, whatever transport an address declared: a ``.onion`` host
    announced as plain ``tcp`` would otherwise be handed to the local resolver.

    In hidden mode a clearnet host never gets a direct connection: it goes through a Tor
    exit if ``routing.tor.CLEARNET_VIA_EXIT`` is set, and is refused otherwise.
    """
    routed = next((b for b in BACKENDS if b.handles_host(host)), None)
    if routed is not None:
        return routed
    if hidden_mode():
        return TOR if clearnet_via_exit() else BLOCKED
    return CLEARNET


def backend_for_transport(transport: str, host: str) -> Optional[RoutingBackend]:
    """The backend that carries a stored address, or None for one nobody can dial (UDP)."""
    transport = str(transport or "").strip().lower()
    if transport and transport != CLEARNET.tag and not routed_host(host):
        return backend_for_tag(transport)  # UDP: nobody dials it
    return backend_for_host(host)


def routed_backend_for_tags(tags: Iterable[str]) -> Optional[RoutingBackend]:
    """The non-clearnet backend a ``Peer.Uri``'s transport tags name, if any.

    Raises ``ValueError`` when the tags mix networks (``tcp`` with ``tor-onion-v3``): an
    old node reads such an address as ``tcp`` and would dial the ``.onion`` name
    directly.
    """
    normalized = {str(t).strip().lower() for t in tags if str(t).strip()}
    routed = [b for b in BACKENDS if b is not CLEARNET and b.tag in normalized]
    if not routed:
        return None
    if len(routed) > 1 or normalized & CLEARNET_TAGS:
        raise ValueError(
            f"transport tags {sorted(normalized)} mix a routing network with another one."
        )
    return routed[0]


def usable(transport: str, host: str) -> bool:
    """Whether this node can dial an address stored with ``transport`` right now."""
    backend = backend_for_transport(transport, host)
    return backend is not None and backend.available()


def announced_onion() -> Optional[Tuple[str, int]]:
    """``(host, port)`` of the onion address this node announces, or None.

    Worked out from the config and ``identity.MNEMONIC`` alone, not from a running Tor:
    the name is fixed by the mnemonic, and a process that is not the daemon (the one
    that publishes the Ergo register) can ask it too. The port is the gateway port.
    """
    from src.identity.node_identity import get_identity_mnemonic
    from src.routing import onion_key
    from src.routing.policy import announce_onion, onion_virtual_port

    mnemonic = get_identity_mnemonic()
    if not announce_onion() or not mnemonic:
        return None
    return onion_key.host_from_mnemonic(mnemonic), onion_virtual_port()


_started = False
_start_lock = threading.Lock()


def start_backends() -> None:
    """Start every backend in the background; a slow one (Tor bootstraps) never delays the node."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    for backend in BACKENDS:
        threading.Thread(target=backend.start, name=f"routing-{backend.name}", daemon=True).start()
    atexit.register(stop_backends)


def stop_backends() -> None:
    for backend in BACKENDS:
        backend.stop()


__all__ = [
    "BACKENDS", "BLOCKED", "announced_onion", "CLEARNET", "TOR", "routed_host", "RoutingUnavailable", "backend_for_host",
    "backend_for_tag", "backend_for_transport", "routed_backend_for_tags", "routed_tags",
    "start_backends", "stop_backends", "usable",
]
