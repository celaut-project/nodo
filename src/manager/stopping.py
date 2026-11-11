"""Stop or hibernate an instance after a notice to its service (#645).

A service that declares ``notice_declaration`` has a virtio console. Before the node stops
or hibernates it for a father (``StopService``, ``nodo kill``, a hibernation that a father
asks for) or for the close of the activity window, it writes ``STOP <t>`` or
``HIBERNATE <t>`` there, and waits until ``<t>`` or until the VM ends. Then it stops or
hibernates the instance as it did before. ``stop_instance`` and ``hibernate_instance`` are
not changed: they stop at once. This module is the part before them.

The stop has two phases:

1. ``begin_notice`` marks the row as stopping (``stopping_until`` and what to do at the
   end), and writes the line. It holds ``instance_lock`` only for this.
2. The end is by the caller that waits (``stop_with_notice``, ``hibernate_with_notice``),
   outside the lock, or by the maintenance tick (``finish_due_stops``) when there is no
   caller that waits: the close of the window, or a caller that is gone.

A stopping instance is not a lost instance. The row says it, so the tick does not score
``INSTANCE_LOST`` when the VM ends by itself, and it ends the stop instead
(``finish_stop``). The instance is alive and the tick charges it as it always does, so the
wait costs what it lasts, and the limit bounds it.

No notice, and the stop at once, for the callers that this does not serve: ``burnall``,
``out_of_balance``, ``process_lost``, ``guest_panic``, ``launch_failed``, the janitor, and an
instance that asks for its own hibernation. They call ``stop_instance`` or
``hibernate_instance`` and never come here.
"""

import threading
import time
from typing import Optional

from src.database.sql_connection import SQLConnection
from src.manager import hibernation
from src.manager.manager import stop_instance
from src.utils import dead_instances, notice
from src.utils import logger as log
from src.utils.instance_lock import instance_lock
from src.virtualizers.microvm.maintain import vm_has_ended

sc = SQLConnection()

STOP = "stop"
HIBERNATE = "hibernate"

#: How often a caller that waits looks at the VM.
POLL_SECONDS = 0.25

#: The tick ends a stop only this long after the limit. A caller that waits ends its own
#: stop at the limit, and this keeps the tick from taking it first. Either of them may
#: end it: both are safe.
TICK_SLACK_SECONDS = 5.0

#: How many callers wait after a notice at the same time (``stop_with_notice`` and
#: ``hibernate_with_notice``). The other callers queue for a place, and their own wait
#: starts when they get it. A caller that waits holds a thread of the gRPC server, so this
#: stays well under the size of its pool.
MAX_CONCURRENT_WAITS = 16

_wait_places = threading.BoundedSemaphore(MAX_CONCURRENT_WAITS)

_VERB = {STOP: notice.STOP, HIBERNATE: notice.HIBERNATE}


def is_stopping(token: str) -> bool:
    """Whether the instance ``token`` has a notice that is not over."""
    try:
        return sc.get_local_stopping(token) is not None
    except Exception:
        return False


def begin_notice(
    token: str,
    kind: str,
    reason: str,
    credit: bool = True,
    *,
    grace_seconds: Optional[int] = None,
) -> Optional[float]:
    """Mark the instance as stopping and write the notice. Returns the limit (unix time).

    None means that there is no notice, and the caller stops the instance at once: the
    wait is 0, the instance is not a local one that runs, or its VM has no console of the
    notice (its service declares none, or it started before this existed). If a notice is
    already out, it returns the limit of that one.

    The service declared a notice, so the node does not stop it before the limit just
    because the line did not arrive: it could be in the middle of a write. The row stays
    marked, the node logs the failure, and the caller waits for the whole limit (or for
    the end of the VM).
    """
    if kind not in _VERB:
        raise ValueError(f"unknown kind of stop: {kind!r}")
    # A failure of the notice never keeps the node from stopping the instance.
    try:
        return _begin_notice(token, kind, reason, credit, grace_seconds)
    except Exception as e:
        log.LOGGER(f"Could not give a notice to {token} ({e}); it is stopped with no notice.")
        try:
            sc.clear_local_stopping(token)
        except Exception:
            pass
        return None


def _begin_notice(
    token: str, kind: str, reason: str, credit: bool, grace_seconds: Optional[int]
) -> Optional[float]:
    grace = notice.stop_grace_seconds() if grace_seconds is None else grace_seconds
    if grace <= 0:
        return None
    if not sc.internal_instance_exists(id=token):
        return None
    with instance_lock(token):
        stopping = sc.get_local_stopping(token)
        if stopping:
            return stopping["until"]
        if not notice.has_device(token):
            return None
        deadline = notice.grace_deadline(grace)
        if not sc.begin_local_stopping(token, deadline, kind, reason, credit):
            return None
        delivered = notice.deliver(token, _VERB[kind], deadline)
    if delivered:
        log.LOGGER(
            f"Wrote {_VERB[kind]} to {token}: the node stops it at {int(deadline)} at the latest."
        )
    else:
        log.LOGGER(
            f"Could not write {_VERB[kind]} to {token}. Its service declared a notice, so the "
            f"node still waits until {int(deadline)} before it stops it."
        )
    return deadline


def wait_until(token: str, deadline: float) -> None:
    """Wait until ``deadline``, or until the VM ends, or until the instance is gone.

    Not called with a lock held. The instance is alive here, and the tick charges it.
    """
    while time.time() < deadline:
        if vm_has_ended(token) or not sc.internal_instance_exists(id=token):
            return
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.time())))


def stop_with_notice(
    token: str,
    credit: bool = True,
    reason: str = dead_instances.MANUAL_KILL,
) -> Optional[int]:
    """``stop_instance``, after a notice when the service has one. Returns the refund.

    For ``StopService`` and ``nodo kill``. A delegated instance has no VM here: the peer
    that runs it writes the notice when it gets the ``StopService`` call, and that call has
    no deadline of its own, so it waits for the wait of the peer.
    """
    with _wait_places:
        deadline = begin_notice(token, STOP, reason, credit)
        if deadline is not None:
            wait_until(token, deadline)
    refund = stop_instance(token=token, credit=credit, reason=reason)
    if refund is None and deadline is not None and not sc.internal_instance_exists(id=token):
        # The tick ended the stop between the wait and this call, and the leftover went
        # to the father there.
        return 0
    return refund


def hibernate_with_notice(
    token: str, reason: str = dead_instances.HIBERNATED
) -> bool:
    """``hibernate_instance``, after a notice when the service has one.

    For a hibernation that a father asks for, and for the activity window. A refusal
    (``HibernationRefused``) comes before any notice, as it does without one. True if the
    instance is hibernated when this returns.
    """
    if not sc.internal_instance_exists(id=token) or sc.is_local_instance_hibernated(id=token):
        return hibernation.hibernate_instance(token, reason=reason)
    hibernation.check_can_hibernate(token)
    with _wait_places:
        deadline = begin_notice(token, HIBERNATE, reason)
        if deadline is not None:
            wait_until(token, deadline)
    if deadline is None:
        return hibernation.hibernate_instance(token, reason=reason)
    try:
        return hibernation.hibernate_instance(token, reason=reason)
    except Exception:
        sc.clear_local_stopping(token)
        raise


def finish_stop(token: str, stopping: dict) -> None:
    """End the stop of a stopping instance, as its row says. Never raises.

    Called by the tick when the limit has passed or the VM ended. Safe if the caller that
    waits ends it too: a stop on an instance that is dead does nothing.
    """
    try:
        if stopping["kind"] == HIBERNATE:
            try:
                hibernation.hibernate_instance(token, reason=stopping["reason"])
                return
            except Exception as e:
                # Refused, or any other failure (the VM ended in a state that the
                # hibernation cannot save, a full disk). The row must not stay marked for
                # the tick to try again at each pass, so the instance is stopped.
                log.LOGGER(f"Instance {token} could not be hibernated ({e}); stopping it.")
                stop_instance(token=token, reason=dead_instances.ACTIVITY_WINDOW)
                return
        stop_instance(
            token=token,
            credit=stopping["credit"],
            reason=stopping["reason"] or dead_instances.MANUAL_KILL,
        )
    except Exception as e:
        log.LOGGER(f"Error ending the stop of {token}: {e}")


def finish_due_stops(now: Optional[float] = None) -> int:
    """End the stop of each instance whose notice is over. Returns how many it ended.

    An instance is over when its limit passed (plus a short slack, see
    ``TICK_SLACK_SECONDS``) or its VM ended. Called on each tick. Never raises: it runs in
    the manager loop.
    """
    now = time.time() if now is None else now
    try:
        rows = sc.get_stopping_local_instances()
    except Exception as e:
        log.LOGGER(f"Error reading the instances that are stopping: {e}")
        return 0
    ended = 0
    for token, stopping in rows.items():
        if now >= stopping["until"] + TICK_SLACK_SECONDS or vm_has_ended(token):
            finish_stop(token, stopping)
            ended += 1
    return ended
