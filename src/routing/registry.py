"""Which backend reaches an address."""

import atexit
import threading
from typing import Iterable, Optional

from src.routing.base import RoutingBackend, RoutingUnavailable
from src.routing.clearnet import ClearnetBackend
from src.routing.tor import TorBackend

CLEARNET = ClearnetBackend()
TOR = TorBackend()

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


def backend_for_host(host: str) -> RoutingBackend:
    """The backend ``host`` has to go through. A ``.onion`` name always goes to Tor.

    Chosen by the name, whatever transport an address declared: a ``.onion`` host
    announced as plain ``tcp`` would otherwise be handed to the local resolver.
    """
    return next((b for b in BACKENDS if b.handles_host(host)), CLEARNET)


def backend_for_transport(transport: str, host: str) -> Optional[RoutingBackend]:
    """The backend that carries a stored address, or None for one nobody can dial (UDP)."""
    routed = backend_for_host(host)
    if routed is not CLEARNET:
        return routed
    transport = str(transport or "").strip().lower()
    if not transport or transport == CLEARNET.tag:
        return CLEARNET
    return backend_for_tag(transport)


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
    "BACKENDS", "CLEARNET", "TOR", "RoutingUnavailable", "backend_for_host",
    "backend_for_tag", "backend_for_transport", "routed_backend_for_tags", "routed_tags",
    "start_backends", "stop_backends", "usable",
]
