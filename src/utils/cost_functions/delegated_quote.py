"""A quote that asks the peers: how long it may wait, and how it stops (#551).

``GetServiceEstimatedCost`` offers the best of the local price and the price of a peer
plus commission. Three rules keep that from turning one question into a flood:

* **A time budget, not a depth.** The caller's gRPC deadline says how long it waits.
  This node keeps a part of it for its own work (``OWN_SHARE``, at least
  ``OWN_MIN_S``) and gives the rest to its peers, as their deadline. Each hop gets less
  time, so the tree of questions stops by itself. Below ``MIN_PEER_TIME_S`` the node
  answers with its local price only.
* **One round per question at a time.** A node that is already asking its peers a
  question does not ask them again. A question that comes back through a loop
  (A asks B, B asks A) gets the local answer at once, instead of a new round that waits
  on the first one. The key of the round (``loop_key``) has no MU in it: each hop
  converts the deposit to the MU of the peer, with a rounding, so a key with the deposit
  does not find the question again. The key of the cache cannot do this either.
* **A round is remembered.** The answers of one round are kept for
  ``network.QUERY_CACHE_PEER_TTL_SECONDS``, by the full question. The same question
  asked again in that time does not ask the peers again (``peer_round``).
* **The quote is an offer.** The price given to a client is kept for
  ``network.QUERY_CACHE_TTL_SECONDS``. If the launch would cost more, the launcher does
  not delegate to that peer (``launch_service``).
"""

import threading
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, Optional

from src.utils.config import ConfigManager
from src.utils.tools.query_cache import (
    HIT, QueryCache, canonical_key, peer_quote_ttl, quote_ttl, with_sorted_hashes,
)

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


def loop_key(service_hash: str, configuration, metadata) -> str:
    """The question without its MU: the same key at each hop of a loop.

    Each hop converts ``initial_mu`` to the MU of the peer, and a node fills it in when
    the client did not give it. So the deposit is not in the key. The rest of the
    configuration goes to the peers unchanged.

    The metadata of a question from a peer is the metadata in the registry of this node,
    because the peers send the hashes only (``SEND_ONLY_HASHES_ASKING_COST``). The
    metadata of a client can be different. Then the loop stops one hop later, on the
    next node.
    """
    without_mu = type(configuration)()
    without_mu.CopyFrom(configuration)
    without_mu.ClearField("initial_mu")
    return canonical_key("delegated-loop", service_hash, without_mu, with_sorted_hashes(metadata))


@contextmanager
def asking_peers_for(key: str) -> Iterator[bool]:
    """True if this is the only round of questions with ``key`` (``loop_key``) now."""
    with _IN_FLIGHT_LOCK:
        first = key not in _IN_FLIGHT
        if first:
            _IN_FLIGHT.add(key)
    try:
        yield first
    finally:
        if first:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT.discard(key)


def _copy_quotes(quotes: Dict[str, object]) -> Dict[str, object]:
    copies = {}
    for peer, cost in quotes.items():
        copies[peer] = type(cost)()
        copies[peer].CopyFrom(cost)
    return copies


def peer_round(
        service_hash: str,
        configuration,
        metadata,
        ask: Callable[[], Dict[str, object]],
) -> Optional[Dict[str, object]]:
    """The price of each peer for this question, or None if a round for it is open now.

    ``configuration`` is the one that goes to the peers, with ``initial_mu`` in local MU.
    ``ask`` asks the peers. Its answers are kept for
    ``network.QUERY_CACHE_PEER_TTL_SECONDS``: they are prices of peers, as the quotes that
    ``estimate_cost_on_peer`` keeps. An empty round is not kept, as an error is not.

    None is the answer to a loop, and also to a second client with the same question
    while the first round is open. The two look the same here, and to wait for the
    first round in a loop is to wait for itself.
    """
    ttl = peer_quote_ttl()
    key = canonical_key(
        "delegated-round", service_hash, configuration, with_sorted_hashes(metadata),
    )
    if ttl > 0:
        status, remembered = QueryCache().recall(key)
        if status == HIT:
            return _copy_quotes(remembered)
    with asking_peers_for(loop_key(service_hash, configuration, metadata)) as first:
        if not first:
            return None
        quotes = ask()
    if quotes and ttl > 0:
        QueryCache().remember(key, _copy_quotes(quotes), ttl)
    return quotes


def _offer_key(client_id: str, service_hash: str) -> str:
    return canonical_key("delegated-offer", str(client_id), str(service_hash))


def remember_offer(client_id: str, service_hash: str, cost_mu: int) -> None:
    """Keep what this node offered ``client_id`` for ``service_hash``, as the start cost."""
    QueryCache().remember(_offer_key(client_id, service_hash), int(cost_mu), quote_ttl())


def recall_offer(client_id: str, service_hash: str) -> Optional[int]:
    """The start cost this node offered, or None if it offered nothing recently."""
    status, value = QueryCache().recall(_offer_key(client_id, service_hash))
    return int(value) if status == HIT else None
