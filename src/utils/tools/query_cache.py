"""A local cache of answers to read-only queries, keyed by what was asked (#456).

``GetServiceEstimatedCost`` and ``GetResourceAvailability`` are questions whose answer
depends only on their content, not on who asks or through whom the question came. So a
node does not need the question to carry a token to know it has met it before: it hashes
what it received. For one key the cache is in one of three states:

- **unknown**: compute the answer, holding the key as *in progress* meanwhile;
- **in progress**: refuse with ``QueryInProgress`` ("retry"). If the computation ever asks
  peers, a question that comes back to this node (A -> B -> A) lands here and stops after
  one lap; an unrelated caller asking the same thing at the same moment is told to retry
  and finds the answer a moment later;
- **done**: answer from memory until the entry expires.

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
    return digest.hexdigest()


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


class QueryCache(metaclass=Singleton):
    """Answers by key, each in progress or done until its expiry. Thread-safe.

    Shared by every gateway thread, so ``begin`` -- the check and the claim -- is atomic:
    two arrivals of one key cannot both find it unknown.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # key -> (value, expires_at); value is _PENDING while being computed.
        self._entries: "OrderedDict[str, Tuple[Any, float]]" = OrderedDict()

    def begin(self, key: str) -> Tuple[str, Any]:
        """``(HIT, answer)``, ``(IN_PROGRESS, None)``, or ``(MISS, None)`` with the key claimed.

        A claimed key must be settled with ``finish`` or ``abort``.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                value, expires_at = entry
                if value is _PENDING:
                    return IN_PROGRESS, None
                if expires_at > now:
                    self._entries.move_to_end(key)
                    return HIT, value
                del self._entries[key]
            self._entries[key] = (_PENDING, 0.0)
            self._evict(now)
            return MISS, None

    def finish(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            if key in self._entries:
                self._entries[key] = (value, time.monotonic() + ttl)
                self._entries.move_to_end(key)

    def recall(self, key: str) -> Tuple[str, Any]:
        """``(HIT, answer)`` or ``(MISS, None)``, claiming nothing: for a node asking a peer.

        An asker does not refuse its own concurrent identical questions (that would drop a
        candidate from a second launch for no reason); it only skips asking again once an
        answer is in hand. Pair with ``remember``.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] is not _PENDING and entry[1] > now:
                self._entries.move_to_end(key)
                return HIT, entry[0]
            return MISS, None

    def remember(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] is _PENDING:
                return
            self._entries[key] = (value, time.monotonic() + ttl)
            self._entries.move_to_end(key)
            self._evict(time.monotonic())

    def abort(self, key: str) -> None:
        """Forget a claimed key whose computation failed: an error is never remembered."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] is _PENDING:
                del self._entries[key]

    def get_or_compute(self, key: str, ttl: float, compute: Callable[[], Any]) -> Any:
        """The remembered answer to ``key``, else ``compute()``'s, remembered for ``ttl`` seconds.

        Raises ``QueryInProgress`` when ``key`` is being computed already. With ``ttl``
        at 0 the cache is off for this question and ``compute`` always runs.
        """
        if ttl <= 0:
            return compute()
        status, value = self.begin(key)
        if status == HIT:
            return value
        if status == IN_PROGRESS:
            raise QueryInProgress(f"Query {key[:16]} is already being answered, retry")
        try:
            value = compute()
        except BaseException:
            self.abort(key)
            raise
        self.finish(key, value, ttl)
        return value

    def _evict(self, now: float) -> None:
        limit = max_entries()
        # Done entries only: dropping a pending one would let a second arrival of the
        # same key compute in parallel, which is what it exists to prevent. Expired
        # ones go first, then the least recently used.
        if len(self._entries) <= limit:
            return
        for k in [k for k, (v, exp) in self._entries.items() if v is not _PENDING and exp <= now]:
            if len(self._entries) <= limit:
                return
            del self._entries[k]
        for k in [k for k, (v, _) in self._entries.items() if v is not _PENDING]:
            if len(self._entries) <= limit:
                return
            del self._entries[k]


_PENDING = object()
