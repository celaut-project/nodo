"""The quote of a peer as the peer gave it, in its own MU (#567).

``estimate_cost_on_peer`` gives the balancer and the launcher the quote of a peer in our
MU. The check of the maintenance charge of a delegated instance needs the figures that
the peer signed, in the MU of the peer: a conversion rounds, and the rate between the two
MU can move. So each quote is kept here as it came, by the peer and by the quote in our
MU that was made from it, and the launcher finds it again when it delegates (``signed``).

In memory, and bounded: a quote that is not found gives a delegated row with no quote,
and the charge of that instance is not checked.
"""
import math
import threading
from collections import OrderedDict
from typing import Optional

MAX_QUOTES = 1024

_quotes: "OrderedDict[tuple, object]" = OrderedDict()
_lock = threading.Lock()


def _key(peer_id: str, local_cost) -> tuple:
    return peer_id, local_cost.SerializeToString(deterministic=True)


def remember(peer_id: str, local_cost, peer_cost) -> None:
    """Keep ``peer_cost``, the quote that ``peer_id`` sent, under ``local_cost``, the
    same quote in our MU. Never raises: a quote that is not kept is not checked."""
    try:
        key = _key(peer_id, local_cost)
        kept = type(peer_cost)()
        kept.CopyFrom(peer_cost)
    except Exception:
        return
    with _lock:
        _quotes[key] = kept
        _quotes.move_to_end(key)
        while len(_quotes) > MAX_QUOTES:
            _quotes.popitem(last=False)


def signed(peer_id: str, local_cost):
    """The quote that ``peer_id`` sent for ``local_cost``, in its MU, or None."""
    try:
        key = _key(peer_id, local_cost)
    except Exception:
        return None
    with _lock:
        found = _quotes.get(key)
    if found is None:
        return None
    copy = type(found)()
    copy.CopyFrom(found)
    return copy


def maintenance_terms(peer_cost) -> Optional[dict]:
    """The maintenance figures of ``peer_cost`` that a delegated row keeps, as the peer
    sent them: ``init_maintenance_cost`` and ``max_maintenance_cost`` in its MU,
    ``maintenance_seconds_loop`` and ``variance``. None with no quote, or when a figure
    cannot be read."""
    if peer_cost is None:
        return None
    try:
        variance = float(peer_cost.variance)
        return {
            "init_maintenance_cost": int(peer_cost.init_maintenance_cost.n or 0),
            "max_maintenance_cost": int(peer_cost.max_maintenance_cost.n or 0),
            "maintenance_seconds_loop": int(peer_cost.maintenance_seconds_loop),
            "variance": variance if math.isfinite(variance) else 0.0,
        }
    except (AttributeError, TypeError, ValueError):
        return None
