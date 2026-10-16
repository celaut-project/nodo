"""A quote that asks the peers: how long it may wait, and how it stops (#551).

``GetServiceEstimatedCost`` offers the best of the local price and the price of a peer
plus commission. Three rules keep that from turning one question into a flood:

* **A time budget, not a depth.** The caller's gRPC deadline says how long it waits.
  This node keeps a part of it for its own work (``OWN_SHARE``, at least
  ``OWN_MIN_S``) and gives the rest to its peers, as their deadline. Each hop gets less
  time, so the tree of questions stops by itself. Below ``MIN_PEER_TIME_S`` the node
  answers with its local price only.
* **One round per service at a time.** A node that is already asking its peers about a
  service does not ask them again for it. A question that comes back through a loop
  (A asks B, B asks A) gets the local answer at once, instead of a new round that waits
  on the first one. The cache key cannot do this: each hop converts the configuration
  to the MU of the peer, so the question comes back with another key.
* **The quote is an offer.** The price given to a client is kept for
  ``network.QUERY_CACHE_TTL_SECONDS``. If the launch would cost more, the launcher does
  not delegate to that peer (``launch_service``).
"""

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from src.utils.config import ConfigManager
from src.utils.tools.query_cache import HIT, QueryCache, canonical_key, quote_ttl

env_manager = ConfigManager()

# The part of the remaining time this node keeps for itself: to price locally, to
# sort the answers, and to send its own. The rest goes to the peers.
OWN_SHARE = 0.2
OWN_MIN_S = 0.2
# Less time than this for the peers, and the node does not ask them.
MIN_PEER_TIME_S = 0.5
DEFAULT_TIMEOUT_S = 10.0

_IN_FLIGHT = set()
_IN_FLIGHT_LOCK = threading.Lock()


def delegation_time_budget(context) -> Optional[float]:
    """Seconds this node may wait for its peers, or None if it must not ask them.

    ``context`` is the gRPC context of the request: its ``time_remaining()`` is the
    deadline the caller sent (``grpc-timeout``). A caller with no deadline gets
    ``EXTERNAL_COST_TIMEOUT``, the time this node gives to a peer itself.
    """
    remaining = None
    try:
        remaining = context.time_remaining() if context is not None else None
    except Exception:
        remaining = None
    if remaining is None:
        try:
            remaining = float(env_manager.get("EXTERNAL_COST_TIMEOUT", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            remaining = DEFAULT_TIMEOUT_S
    budget = remaining - max(remaining * OWN_SHARE, OWN_MIN_S)
    return budget if budget >= MIN_PEER_TIME_S else None


@contextmanager
def asking_peers_for(service_hash: str) -> Iterator[bool]:
    """True if this is the only round of questions about ``service_hash`` now."""
    with _IN_FLIGHT_LOCK:
        first = service_hash not in _IN_FLIGHT
        if first:
            _IN_FLIGHT.add(service_hash)
    try:
        yield first
    finally:
        if first:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT.discard(service_hash)


def _offer_key(client_id: str, service_hash: str) -> str:
    return canonical_key("delegated-offer", str(client_id), str(service_hash))


def remember_offer(client_id: str, service_hash: str, cost_mu: int) -> None:
    """Keep what this node offered ``client_id`` for ``service_hash``, as the start cost."""
    QueryCache().remember(_offer_key(client_id, service_hash), int(cost_mu), quote_ttl())


def recall_offer(client_id: str, service_hash: str) -> Optional[int]:
    """The start cost this node offered, or None if it offered nothing recently."""
    status, value = QueryCache().recall(_offer_key(client_id, service_hash))
    return int(value) if status == HIT else None
