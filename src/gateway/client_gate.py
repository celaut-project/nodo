"""Requires a client_id before a Gateway RPC does anything else (issue #428).

Every RPC but ``GenerateClient`` used to be reachable by anyone who could open a gRPC
connection, with no identity and no cost -- ``IntroducePeer`` was the sharpest case
(a signed announcement is free to mint and still costs this node a DB write per call),
but the same shape applied to the rest: nothing distinguished one caller from another,
so nothing could be rate-limited.

The fix generalizes a mechanism this node already has in the other direction.
``get_client_id_on_other_peer`` (``src/manager/manager.py``) mints a ``client_id`` at a
peer before calling that peer's billable RPCs; this module is the inbound half of the
same idea. ``client_id`` -- not the caller's IP -- is the identity to key on: an IP is
free to rotate and tells this node little (NAT, shared egress, a busy hub dialled by
many operators), while a ``client_id`` is free only up to
``free_tier.MAX_WORK_FREE_CLIENTS_PER_DIFFICULTY`` and increasingly proof-of-work-gated
after (issue #361) -- exactly the property a Sybil-resistant identifier needs.

Two exemptions, and nothing else:

- A caller that is a locally-running instance of this node, identified by its address
  the same way ``ModifyServiceSystemResources`` already does
  (``get_internal_service_id_by_uri``). It isn't an external caller, so there is
  nothing here to rate-limit it against beyond what its own balance already prices.
- ``GenerateClient`` itself, which is how a caller gets a ``client_id`` in the first
  place and is therefore never gated by this module.

Everything else needs a ``client_id`` this node already minted, and is then subject to
the shared rate limiter below -- one sliding window per ``client_id``, across every
gated RPC together (not a separate budget per method: ``IntroducePeer`` and
``GetMetrics`` don't need independent bookkeeping to make the point).
"""
import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Optional, Type, Union

from google.protobuf.message import Message

from protos import celaut_pb2
from src.database.sql_connection import SQLConnection
from src.gateway.client_pow import is_uuid4_hex
from src.manager.manager import get_internal_service_id_by_uri
from src.utils import logger as log
from src.utils.bee_client import CLIENT_INDEX, BeeClient
from src.utils.config import ConfigManager
from src.utils.utils import get_only_the_ip_from_context

env_manager = ConfigManager()
sc = SQLConnection()

# Bounds how many distinct client_ids the sliding window remembers at once. A caller
# cannot dodge the window by discarding a client_id for a fresh one -- minting one is
# priced by issue #361 -- so this exists only to cap memory against however many
# distinct clients are legitimately active, the same way utils.is_open's cache is
# capped against however many addresses peers have announced.
_MAX_TRACKED_CLIENTS = 10_000


class ClientRequired(Exception):
    """No usable identity: no local instance, and no valid, unthrottled client_id."""


def _config_number(key: str, default: float) -> float:
    try:
        return float(env_manager.get(key, default))
    except (TypeError, ValueError):
        return default


def _window_seconds() -> float:
    return _config_number("communication.CLIENT_RATE_LIMIT_WINDOW_SECONDS", 60)


def _max_calls_per_window() -> int:
    return int(_config_number("communication.CLIENT_RATE_LIMIT_MAX_CALLS", 120))


def _quarantine_seconds() -> float:
    return _config_number("communication.CLIENT_RATE_LIMIT_QUARANTINE_SECONDS", 300)


class _ClientCallWindow:
    """One shared sliding window of recent calls, keyed by client_id.

    In-memory only, on purpose: the rate limiting itself must cost this node no I/O
    (identifying the caller first -- a client_id existence check, or the local-instance
    lookup -- is a separate, cheap indexed read, the same one ModifyServiceSystemResources
    already pays; this class starts after that). The gateway serves every RPC on its
    own thread from a shared pool, so the state below is guarded by a lock rather than
    assumed single-threaded.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._quarantined_until: "OrderedDict[str, float]" = OrderedDict()

    def allow(self, client_id: str) -> bool:
        now = time.monotonic()
        window = _window_seconds()
        with self._lock:
            until = self._quarantined_until.get(client_id)
            if until is not None:
                if now < until:
                    self._quarantined_until.move_to_end(client_id)
                    return False
                del self._quarantined_until[client_id]

            calls = self._calls.get(client_id)
            if calls is None:
                calls = deque()
                self._calls[client_id] = calls
            self._calls.move_to_end(client_id)

            cutoff = now - window
            while calls and calls[0] < cutoff:
                calls.popleft()

            if len(calls) >= _max_calls_per_window():
                del self._calls[client_id]
                self._quarantined_until[client_id] = now + _quarantine_seconds()
                self._quarantined_until.move_to_end(client_id)
                while len(self._quarantined_until) > _MAX_TRACKED_CLIENTS:
                    # Same eviction as _calls below: a client_id that has been
                    # quarantined longest without asking again is what goes, not
                    # necessarily the one whose quarantine expires soonest -- an
                    # attacker minting fresh ids to fill this up still pays for each
                    # one (issue #361), so eviction here is a memory cap, not a
                    # pardon.
                    self._quarantined_until.popitem(last=False)
                log.LOGGER(
                    f"Client {client_id} quarantined for {_quarantine_seconds():.0f}s: "
                    f"more than {_max_calls_per_window()} calls in {window:.0f}s."
                )
                return False

            calls.append(now)

            while len(self._calls) > _MAX_TRACKED_CLIENTS:
                # Oldest-inserted, not oldest-called: move_to_end above keeps a client
                # that is still calling at the recent end, so this evicts whichever
                # client_id has gone longest without a call of its own.
                self._calls.popitem(last=False)

            return True


_window = _ClientCallWindow()


def require_caller(context, client_id: str = "") -> str:
    """The identity an RPC may proceed under, or raise ``ClientRequired``.

    Returns the local instance's own token, or the caller's ``client_id`` -- either
    way, something the caller of this function can log or bill against.
    """
    caller_ip = get_only_the_ip_from_context(context_peer=context.peer())
    local_instance = get_internal_service_id_by_uri(uri=caller_ip)
    if local_instance:
        return local_instance

    # Shape-check before the DB: free for an attacker to get right, but it costs
    # nothing to check either, and it is what keeps a flood of plainly-fabricated
    # strings (empty, wrong length, non-hex) from reaching sc.client_exists at all.
    # A client_id that does look like a UUID4 still costs one indexed read -- the
    # same one ModifyServiceSystemResources already pays per call -- because there is
    # no way to tell a minted id from a guessed one without it.
    if not is_uuid4_hex(client_id) or not sc.client_exists(client_id=client_id):
        raise ClientRequired(
            "This RPC requires a client_id minted by GenerateClient first."
        )

    if not _window.allow(client_id):
        raise ClientRequired(
            f"Client {client_id} is calling too fast and is temporarily quarantined."
        )

    return client_id


def parse_with_client(
        request_iterator,
        payload_type: Union[Type[Message], Message],
        payload_index: int = 1,
) -> "tuple[Optional[Message], str]":
    """Parse a request stream that may carry ``payload_type`` and/or a ``Client``.

    Returns ``(payload_or_None, client_id)`` -- ``client_id`` is ``""`` when no
    ``Client`` was sent, the same empty-string convention ``require_caller`` and
    ``generate_client_or_pow_required`` already use elsewhere. Order on the wire is
    the caller's choice, same as every other multi-message envelope in this codebase
    (``StartService``'s, ``protos/gateway_bee.py``).
    """
    payload = None
    client_id = ""
    for r in BeeClient.parse(
            request_iterator,
            indices={payload_index: payload_type, CLIENT_INDEX: celaut_pb2.Client},
    ):
        if isinstance(r, celaut_pb2.Client):
            client_id = r.client_id
        elif isinstance(r, payload_type):
            payload = r
    return payload, client_id
