"""A local cache of answers to read-only queries, keyed by what was asked (#456).

``GetServiceEstimatedCost`` and ``GetResourceAvailability`` are questions whose answer
depends only on their content, not on who asks or through whom the question came. So a
node does not need the question to carry a token to know it has met it before: it hashes
what it received. For one key the cache is in one of three states:

- **unknown**: compute the answer, holding the key as *in progress* meanwhile;
- **in progress**: wait for the answer being computed, up to
  ``network.QUERY_CACHE_WAIT_SECONDS`` (single-flight), and serve it; past that, or when
  the thread computing it asks again itself, refuse with ``QueryInProgress`` ("retry").
  So two launches asking the same thing at once both get the answer, while a question
  that comes back to this node (A -> B -> A, were the computation ever to ask peers)
  stops after one lap;
- **done**: answer from memory until the entry expires, or until ``invalidate`` drops it
  because what it answered about changed here (a local instance started, stopped or was
  resized).

Nothing in it is a proof or needs anyone else to cooperate: a node protects itself with
what it observes. A forwarder that changes the content of a question gets it computed
again, which is what a different question deserves. What bounds an attacker who varies it
on purpose is the price of each call, not this cache.

StartService does not use it: running a service is not an idempotent answer, and two
clients may legitimately start the same one at once. It keeps the ``RecursionGuard``
(``recursion_guard.py``).
"""
import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional, Tuple

from google.protobuf.message import Message

from src.utils.singleton import Singleton

DEFAULT_MAX_ENTRIES = 4096

HIT = "hit"
IN_PROGRESS = "in_progress"
MISS = "miss"


class QueryInProgress(Exception):
    """The same question is being answered here right now: retry shortly."""


def max_entries() -> int:
    """``network.QUERY_CACHE_MAX_ENTRIES``: how many questions are remembered at once."""
    from src.utils.config import ConfigManager
    try:
        value = int(ConfigManager().get("network.QUERY_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ENTRIES
    return value if value >= 1 else DEFAULT_MAX_ENTRIES


def ttl_seconds(key: str, default: float) -> float:
    """A ``network.*`` TTL in seconds; 0 (or anything unreadable or negative) disables the cache."""
    from src.utils.config import ConfigManager
    try:
        value = float(ConfigManager().get(key, default))
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def wait_seconds() -> float:
    """``network.QUERY_CACHE_WAIT_SECONDS``: how long an identical question waits for one in progress."""
    return ttl_seconds("network.QUERY_CACHE_WAIT_SECONDS", 5)


def peer_quote_ttl() -> float:
    """``network.QUERY_CACHE_PEER_TTL_SECONDS``: how long a quote got from a peer is reused.

    Shorter than ``quote_ttl``: the peer may already have served it from its own cache.
    """
    return ttl_seconds("network.QUERY_CACHE_PEER_TTL_SECONDS", 10)


def quote_ttl() -> float:
    """``network.QUERY_CACHE_TTL_SECONDS``: how long a price quote is remembered."""
    return ttl_seconds("network.QUERY_CACHE_TTL_SECONDS", 30)


def availability_ttl() -> float:
    """``network.QUERY_CACHE_AVAILABILITY_TTL_SECONDS``: short, availability moves fast."""
    return ttl_seconds("network.QUERY_CACHE_AVAILABILITY_TTL_SECONDS", 5)


def canonical_key(namespace: str, *parts: Any) -> str:
    """The key of a question: ``namespace`` plus every part, in a form that cannot be varied.

    A part is a protobuf message or a string. A message is read back from its own bytes
    and stripped of the fields this node does not know, then serialized deterministically
    (map entries sorted), so neither the order the sender put its fields in nor fields
    this node would ignore anyway produce another key. A value is hashed exactly as it
    came: rounding it into buckets would change the answer.

    The namespace is kept in clear in front of the digest, so ``invalidate`` can drop
    every answer to one RPC.
    """
    digest = hashlib.sha256()
    digest.update(namespace.encode())
    for part in parts:
        if isinstance(part, Message):
            copy = type(part).FromString(part.SerializeToString())
            copy.DiscardUnknownFields()
            data = type(part).DESCRIPTOR.full_name.encode() + b"\0" \
                + copy.SerializeToString(deterministic=True)
        elif isinstance(part, str):
            data = b"str\0" + part.encode()
        elif part is None:
            data = b"none"
        else:
            raise TypeError(f"Cannot build a query key from {type(part).__name__}")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return f"{namespace}:{digest.hexdigest()}"


def with_sorted_hashes(metadata: Optional[Message]) -> Optional[Message]:
    """A ``Metadata`` with its hashes in a fixed order, so the same set always keys alike.

    The gateway parser collects them in a set, whose order is arbitrary.
    """
    if metadata is None:
        return None
    copy = type(metadata)()
    copy.CopyFrom(metadata)
    hashes = sorted(copy.hashtag.hash, key=lambda h: (h.type, h.value))
    copy.hashtag.ClearField("hash")
    copy.hashtag.hash.extend(hashes)
    return copy


class _Pending:
    """A key being computed: who computes it, and an event its waiters block on."""

    __slots__ = ("event", "owner", "stale")

    def __init__(self):
        self.event = threading.Event()
        self.owner = threading.get_ident()
        # Set by `invalidate` while computing: the answer goes to its own caller only.
        self.stale = False


class QueryCache(metaclass=Singleton):
    """Answers by key, each in progress or done until its expiry. Thread-safe.

    Shared by every gateway thread, so ``begin`` -- the check and the claim -- is atomic:
    two arrivals of one key cannot both find it unknown.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # key -> (value, expires_at); value is a _Pending while being computed.
        self._entries: "OrderedDict[str, Tuple[Any, float]]" = OrderedDict()

    def begin(self, key: str) -> Tuple[str, Any]:
        """``(HIT, answer)``, ``(IN_PROGRESS, pending)``, or ``(MISS, None)`` with the key claimed.

        A claimed key must be settled with ``finish`` or ``abort``.
        """
        limit = max_entries()
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                value, expires_at = entry
                if isinstance(value, _Pending):
                    return IN_PROGRESS, value
                if expires_at > now:
                    self._entries.move_to_end(key)
                    return HIT, value
                del self._entries[key]
            self._entries[key] = (_Pending(), 0.0)
            self._evict(now, limit)
            return MISS, None

    def finish(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or not isinstance(entry[0], _Pending):
                return
            pending = entry[0]
            if pending.stale:
                del self._entries[key]
            else:
                self._entries[key] = (value, time.monotonic() + ttl)
                self._entries.move_to_end(key)
        pending.event.set()

    def recall(self, key: str) -> Tuple[str, Any]:
        """``(HIT, answer)`` or ``(MISS, None)``, claiming nothing: for a node asking a peer.

        An asker does not hold back its own concurrent identical questions; it only skips
        asking again once an answer is in hand. Pair with ``remember``.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and not isinstance(entry[0], _Pending) and entry[1] > now:
                self._entries.move_to_end(key)
                return HIT, entry[0]
            return MISS, None

    def remember(self, key: str, value: Any, ttl: float) -> None:
        limit = max_entries()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and isinstance(entry[0], _Pending):
                return
            now = time.monotonic()
            self._entries[key] = (value, now + ttl)
            self._entries.move_to_end(key)
            self._evict(now, limit)

    def abort(self, key: str) -> None:
        """Forget a claimed key whose computation failed: an error is never remembered.

        Its waiters wake up and find the key unknown, so one of them computes it.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or not isinstance(entry[0], _Pending):
                return
            del self._entries[key]
        entry[0].event.set()

    def invalidate(self, namespace: str) -> None:
        """Forget every answer to ``namespace``: what they answered about has changed here.

        One still being computed may have read the old state, so it is marked not to be
        remembered: its own caller gets it, and anyone waiting on it computes afresh.
        """
        prefix = f"{namespace}:"
        with self._lock:
            for k in [k for k in self._entries if k.startswith(prefix)]:
                value = self._entries[k][0]
                if isinstance(value, _Pending):
                    value.stale = True
                else:
                    del self._entries[k]

    def get_or_compute(self, key: str, ttl: float, compute: Callable[[], Any]) -> Any:
        """The remembered answer to ``key``, else ``compute()``'s, remembered for ``ttl`` seconds.

        When ``key`` is being computed already, waits for that answer (up to
        ``wait_seconds()``) instead of computing it twice. Raises ``QueryInProgress`` when
        the wait runs out, or at once when this very thread is the one computing it -- a
        question that came back to the node asking it. With ``ttl`` at 0 the cache is off
        for this question and ``compute`` always runs.
        """
        if ttl <= 0:
            return compute()
        deadline = time.monotonic() + wait_seconds()
        while True:
            status, value = self.begin(key)
            if status == HIT:
                return value
            if status == MISS:
                break
            if value.owner == threading.get_ident():
                raise QueryInProgress(f"Query {key[:48]} came back to the thread answering it")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not value.event.wait(remaining):
                raise QueryInProgress(f"Query {key[:48]} is still being answered, retry")
            # Settled: served from memory on the next `begin`, or -- aborted, or computed
            # from a state since invalidated -- unknown again, so this caller computes it.
        try:
            value = compute()
        except BaseException:
            self.abort(key)
            raise
        self.finish(key, value, ttl)
        return value

    def _evict(self, now: float, limit: int) -> None:
        # Done entries only: dropping a pending one would let a second arrival of the
        # same key compute in parallel, which is what it exists to prevent. Expired
        # ones go first, then the least recently used.
        if len(self._entries) <= limit:
            return
        for k in [k for k, (v, exp) in self._entries.items() if not isinstance(v, _Pending) and exp <= now]:
            if len(self._entries) <= limit:
                return
            del self._entries[k]
        for k in [k for k, (v, _) in self._entries.items() if not isinstance(v, _Pending)]:
            if len(self._entries) <= limit:
                return
            del self._entries[k]
