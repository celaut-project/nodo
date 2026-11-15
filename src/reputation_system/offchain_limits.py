"""Limits on who may take reputation proofs from this node, and who may give them (#652).

``GetReputationProofs`` and ``PushReputationProof`` sit behind ``client_gate.require_caller``,
which gives each caller a ``client_id`` and counts calls. That does not bound the work of
these two RPCs, because one call can move up to a whole table of proofs. So each RPC has its
own limits, all in ``reputation.*`` and all in memory (a restart clears them):

* per call: at most ``MAX_OFFCHAIN_BYTES_PER_RESPONSE`` bytes in an answer, and at most
  ``MAX_OFFCHAIN_PROOFS_PER_RESPONSE`` proofs (``offchain_proof.serve``);
* per client: calls and bytes in a sliding window (``PULL_*`` and ``PUSH_*``);
* for all clients together: calls in a minute (``*_GLOBAL_CALLS_PER_MINUTE``). A client costs
  work to make, but not much for the first ones (``client_pow``), so one operator with many
  clients would otherwise get many budgets.

A refusal costs nothing: it is decided before the node reads a proof from the database or
parses one from the stream. A limit of ``0`` refuses all (a limit is never "no limit").
"""
import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Optional, Tuple

from src.utils.config import ConfigManager

env_manager = ConfigManager()

HOUR = 3600.0
DAY = 86400.0
MINUTE = 60.0

# Bounds how many client ids a meter remembers: a memory cap, not a pardon. A client that is
# dropped starts a new window, but making a client costs proof of work (``client_pow``).
MAX_TRACKED_CLIENTS = 10_000

DEFAULTS = {
    "MAX_OFFCHAIN_BYTES_PER_RESPONSE": 4 * 1024 * 1024,
    "PULL_CALLS_PER_CLIENT_PER_HOUR": 24,
    "PULL_BYTES_PER_CLIENT_PER_HOUR": 16 * 1024 * 1024,
    "PULL_GLOBAL_CALLS_PER_MINUTE": 60,
    "ACCEPT_OFFCHAIN_PUSH": True,
    "PUSH_ONLY_FROM_PEERS": True,
    "PUSH_CALLS_PER_CLIENT_PER_HOUR": 12,
    "PUSH_BYTES_PER_CLIENT_PER_DAY": 2 * 1024 * 1024,
    "PUSH_GLOBAL_CALLS_PER_MINUTE": 60,
}


class Refused(Exception):
    """The caller is over a limit. The message says which, for the log and the caller."""


def setting(key: str):
    """``reputation.<key>``, or the default when it is missing or not of the type of it."""
    default = DEFAULTS[key]
    value = env_manager.get(f"reputation.{key}", default)
    if value is None:
        return default
    if isinstance(default, bool):
        return bool(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


class Meter:
    """Amounts in a sliding window, for each key. Thread safe."""

    def __init__(self, window: float, clock=time.monotonic):
        self._window = float(window)
        self._clock = clock
        self._lock = threading.Lock()
        self._events: "OrderedDict[str, Deque[Tuple[float, int]]]" = OrderedDict()

    def _trim(self, key: str, now: float) -> Deque[Tuple[float, int]]:
        events = self._events.get(key)
        if events is None:
            events = deque()
            self._events[key] = events
        self._events.move_to_end(key)
        cutoff = now - self._window
        while events and events[0][0] <= cutoff:
            events.popleft()
        while len(self._events) > MAX_TRACKED_CLIENTS:
            self._events.popitem(last=False)
        return events

    def used(self, key: str) -> int:
        with self._lock:
            return sum(amount for _, amount in self._trim(key, self._clock()))

    def add(self, key: str, amount: int = 1) -> None:
        if amount <= 0:
            return
        with self._lock:
            now = self._clock()
            self._trim(key, now).append((now, int(amount)))

    def take(self, key: str, limit: int, amount: int = 1) -> bool:
        """Count ``amount`` for ``key`` when that keeps it within ``limit``. One step."""
        with self._lock:
            now = self._clock()
            events = self._trim(key, now)
            if sum(a for _, a in events) + amount > limit:
                return False
            events.append((now, int(amount)))
            return True

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


_GLOBAL = "*"
_pull_calls = Meter(HOUR)
_pull_bytes = Meter(HOUR)
_pull_global = Meter(MINUTE)
_push_calls = Meter(HOUR)
_push_bytes = Meter(DAY)
_push_global = Meter(MINUTE)


def reset() -> None:
    """Forget every count. For tests."""
    for meter in (_pull_calls, _pull_bytes, _pull_global, _push_calls, _push_bytes,
                  _push_global):
        meter.clear()


# -- Pull ----------------------------------------------------------------------------------

def admit_pull(client: str) -> int:
    """Count one call of ``GetReputationProofs`` by ``client``. Returns the most bytes that
    the answer may hold, or raises ``Refused``. Call ``charge_pull`` with the bytes sent."""
    if not _pull_global.take(_GLOBAL, setting("PULL_GLOBAL_CALLS_PER_MINUTE")):
        raise Refused("This node is giving reputation proofs to too many callers now.")
    if not _pull_calls.take(client, setting("PULL_CALLS_PER_CLIENT_PER_HOUR")):
        raise Refused(f"Client {client} asks for reputation proofs too often.")
    left = setting("PULL_BYTES_PER_CLIENT_PER_HOUR") - _pull_bytes.used(client)
    if left <= 0:
        raise Refused(f"Client {client} has taken all the reputation proof bytes it may "
                      "take for now.")
    return min(setting("MAX_OFFCHAIN_BYTES_PER_RESPONSE"), left)


def charge_pull(client: str, sent: int) -> None:
    _pull_bytes.add(client, sent)


# -- Push ----------------------------------------------------------------------------------

def admit_push(client: str, is_peer: Optional[bool]) -> int:
    """Count one call of ``PushReputationProof`` by ``client``. Returns the most bytes that
    the call may carry, or raises ``Refused``. ``is_peer`` is whether the caller is a peer
    of this node (None: not known, and then it is not one)."""
    if not setting("ACCEPT_OFFCHAIN_PUSH"):
        raise Refused("This node does not accept pushed reputation proofs.")
    if setting("PUSH_ONLY_FROM_PEERS") and not is_peer:
        raise Refused("This node accepts pushed reputation proofs from its peers only.")
    if not _push_global.take(_GLOBAL, setting("PUSH_GLOBAL_CALLS_PER_MINUTE")):
        raise Refused("This node is taking pushed reputation proofs from too many callers "
                      "now.")
    if not _push_calls.take(client, setting("PUSH_CALLS_PER_CLIENT_PER_HOUR")):
        raise Refused(f"Client {client} pushes reputation proofs too often.")
    left = setting("PUSH_BYTES_PER_CLIENT_PER_DAY") - _push_bytes.used(client)
    if left <= 0:
        raise Refused(f"Client {client} has pushed all the reputation proof bytes it may "
                      "push for now.")
    return left


def charge_push(client: str, received: int) -> None:
    _push_bytes.add(client, received)
