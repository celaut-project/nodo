"""Off-chain reputation, as one bounded number per peer for the balancer (#652).

The off-chain proofs of other nodes (``offchain_proof``) cost nothing to publish, so a
proof is worth only what its publisher is worth to this node. Each publisher ``p`` has a
voice ``w(p)``, and its score of a subject ``s`` is read on the scale of our own score:

    v(p, s) = sign * amount / (amount + REPUTATION_HALF_CREDIT)        in (-1, 1)
    w(p)    = cred(p) * max(0, r_local(p))                             in [0, 1)
    s(s)    = sum_p w(p) * v(p, s) / (sum_p w(p) + OFFCHAIN_REPUTATION_PRIOR)

``cred(p)`` is the cosine between the scores of ``p`` and ours, as for the on-chain term
(``onchain_credit.agreement``). ``r_local(p)`` is our own bounded score of the publisher:
a node that did no work for us, or that failed us, has no voice, so new identities cost
nothing and give nothing. The prior keeps one weak voice from moving a peer far.

``w`` and ``v`` are computed on the periodic tick (``offchain_indexer``) and stored, so a
routing decision reads one table and does no network I/O.
"""
import threading
import time
from typing import Dict, Iterable, Mapping, Optional

from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

DEFAULT_OFFCHAIN_WEIGHT = 1.0
DEFAULT_OFFCHAIN_PRIOR = 1.0
CACHE_SECONDS = 60


def _parameter(key: str, default: float) -> float:
    value = ConfigManager().get(f"balancers.{key}", default)
    try:
        return float(default if value is None else value)
    except (TypeError, ValueError):
        LOGGER(f"[BALANCERS] balancers.{key} is not a number ({value!r}); using {default}.")
        return default


def weight() -> float:
    return max(0.0, _parameter("OFFCHAIN_REPUTATION_WEIGHT", DEFAULT_OFFCHAIN_WEIGHT))


def prior() -> float:
    value = _parameter("OFFCHAIN_REPUTATION_PRIOR", DEFAULT_OFFCHAIN_PRIOR)
    return value if value > 0 else DEFAULT_OFFCHAIN_PRIOR


def verdict(amount: int, positive: bool, half: float) -> float:
    """The score of a publisher on the scale of our own: ``r / (|r| + half)``."""
    amount = abs(int(amount))
    if not amount or half <= 0:
        return 0.0
    value = amount / (amount + half)
    return value if positive else -value


def standing(rows: Iterable[Mapping], prior_weight: float) -> Dict[str, float]:
    """``{subject: s}`` from the stored rows (``subject_id``, ``publisher``, ``verdict``,
    ``weight``). A subject that no publisher with a voice speaks of is not in it."""
    voices: Dict[str, float] = {}
    sums: Dict[str, float] = {}
    for row in rows:
        w = float(row["weight"])
        if w <= 0 or row["publisher"] == row["subject_id"]:
            continue
        subject = row["subject_id"]
        voices[subject] = voices.get(subject, 0.0) + w
        sums[subject] = sums.get(subject, 0.0) + w * float(row["verdict"])
    return {subject: sums[subject] / (voices[subject] + prior_weight) for subject in sums}


_lock = threading.Lock()
_cache: Dict[str, object] = {"at": 0.0, "value": None}


def standing_by_peer() -> Dict[str, float]:
    """``{peer_id: s}`` for the balancer, cached for ``CACHE_SECONDS``. ``{}`` when the
    table cannot be read: every candidate then scores 0, never only some of them."""
    with _lock:
        now = time.monotonic()
        cached: Optional[Dict[str, float]] = _cache["value"]  # type: ignore[assignment]
        if cached is not None and now - float(_cache["at"]) < CACHE_SECONDS:
            return cached
    try:
        from src.database.sql_connection import SQLConnection
        value = standing(SQLConnection().get_offchain_opinions(), prior())
    except Exception as e:
        LOGGER(f"Cannot read the off-chain reputation: {e}")
        return {}
    with _lock:
        _cache.update(at=time.monotonic(), value=value)
    return value


def forget_cached_standings() -> None:
    with _lock:
        _cache.update(at=0.0, value=None)
