"""Cycle and depth control for RPCs that re-delegate the same question to peers (#456).

A request that one node can answer by asking its peers the same thing -- StartService
through the balancer and `delegate_execution`, GetServiceEstimatedCost once it compares
peer quotes, GetResourceAvailability once it probes peers -- carries a
``celaut_pb2.RecursionGuard`` alongside it. Two things travel in that message:

- ``token``: an opaque id for the whole request tree. Every node that is answering a
  request registers its token here for as long as it is answering, and refuses a second
  request with a token it already holds: A -> B -> A comes back to A with A's own token,
  and is refused there (``RecursionLoop``). A request without one is a new root and gets
  a fresh token (uuid4 hex).
- ``remaining_hops`` (optional): how many nodes the tree may still enter, counting the
  one receiving it. A receiver given 0 refuses (``RecursionDepthExhausted``); one given
  ``h`` forwards ``h - 1``, and does not forward at all once that would be 0. Absent --
  a root request, or one relayed by a node that predates the field -- means this node's
  own ``network.RECURSION_MAX_HOPS``, and a value above that is clamped to it: a caller
  can spend less of this node's budget than the node would allow, never more.

Nothing here is a proof: a node that wants to can drop or reset both fields before
forwarding. What a rational peer gains and loses by doing so is the subject of
``docs/proposals/456-recursion-guard-incentives.md``; ``docs/RECURSION_GUARD.md`` lists
which RPCs carry the guard and where.
"""
import re
import threading
import uuid
from typing import Dict, Optional

from src.utils.singleton import Singleton

DEFAULT_MAX_HOPS = 16

# Every token this codebase mints is a uuid4 hex; callers that bring their own
# (`nodo force_execution`, the benchmark core service, tests) use the same alphabet.
# Bounded because the registry keeps each one in memory for as long as the request
# lasts, and a token is chosen by whoever sends it.
MAX_TOKEN_LENGTH = 128
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._:-]+")


class RecursionRefused(Exception):
    """The request was refused by the recursion guard, never by the work it asked for."""


class RecursionLoop(RecursionRefused):
    """The token is already being answered on this node: the request came back here."""


class RecursionDepthExhausted(RecursionRefused):
    """The request tree has no hops left to enter this node."""


class MalformedRecursionToken(RecursionRefused):
    """The token is not something this node would ever have minted or forwarded."""


def max_hops() -> int:
    """``network.RECURSION_MAX_HOPS``: how many nodes a request tree rooted here may span."""
    from src.utils.config import ConfigManager
    try:
        value = int(ConfigManager().get("network.RECURSION_MAX_HOPS", DEFAULT_MAX_HOPS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_HOPS
    return value if value >= 1 else DEFAULT_MAX_HOPS


def validate_token(token: str) -> str:
    """``token`` unchanged, or ``MalformedRecursionToken``."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH \
            or not _TOKEN_PATTERN.fullmatch(token):
        raise MalformedRecursionToken(
            f"Malformed recursion token {str(token)[:MAX_TOKEN_LENGTH]!r}: expected 1-"
            f"{MAX_TOKEN_LENGTH} characters of [A-Za-z0-9._:-]."
        )
    return token


class Registry(metaclass=Singleton):
    """The tokens this node is answering right now, each with its remaining hops.

    Shared by every gateway thread, so the check-and-add that detects a loop is atomic:
    two arrivals of the same token cannot both find it absent.
    """

    def __init__(self):
        self.tokens: Dict[str, Optional[int]] = {}
        self._lock = threading.Lock()

    def add(self, token, hops: Optional[int] = None):
        with self._lock:
            if token in self.tokens:
                raise RecursionLoop(f'Block recursion loop, recursion token: {token}')
            self.tokens[token] = hops

    def delete(self, token):
        if token:
            with self._lock:
                self.tokens.pop(token, None)

    def forward_hops(self, token: Optional[str]) -> Optional[int]:
        """The ``remaining_hops`` to send with ``token`` to the next node.

        None when this node holds no budget for it (it is not answering that token,
        or answering it without a guard), so the field is left out and the next node
        applies its own maximum -- exactly what a node predating #456 causes.
        """
        if not token:
            return None
        with self._lock:
            hops = self.tokens.get(token)
        return None if hops is None else max(hops - 1, 0)

    def can_forward(self, token: Optional[str]) -> bool:
        """Whether a request under ``token`` may still be passed on to a peer."""
        hops = self.forward_hops(token)
        return hops is None or hops > 0


def recursion_guard_message(token: Optional[str]):
    """The ``RecursionGuard`` to send downstream with ``token``, or None without one.

    The one place a forwarded guard is built, so every RPC that passes a request on
    decrements the budget the same way. ``token`` is forwarded unchanged.
    """
    if not token:
        return None
    from protos import celaut_pb2
    message = celaut_pb2.RecursionGuard(token=token)
    hops = Registry().forward_hops(token)
    if hops is not None:
        message.remaining_hops = hops
    return message


def received_hops(message) -> Optional[int]:
    """``remaining_hops`` off a received ``RecursionGuard``, None when the sender left it out."""
    return message.remaining_hops if message.HasField("remaining_hops") else None


class RecursionGuard(object):
    """Registers the request's token for the duration of a ``with`` block.

    ``generate=False`` turns the guard off (no token, nothing registered): that is how
    `launch_service` treats a launch asked for by one of this node's own instances,
    which starts a new tree downstream rather than continuing the caller's.
    """

    def __init__(self, token: str, generate: bool, remaining_hops: Optional[int] = None):
        if generate:
            self.token = validate_token(token) if token else uuid.uuid4().hex

            limit = max_hops()
            hops = limit if remaining_hops is None else min(int(remaining_hops), limit)
            if hops <= 0:
                raise RecursionDepthExhausted(
                    f'Recursion depth exhausted, recursion token: {self.token}'
                )

            Registry().add(self.token, hops)

        else:
            self.token = None

    def __enter__(self):
        return self.token

    def __exit__(self, exception_type, exception_value, traceback):
        Registry().delete(self.token)
