"""Hibernate an instance by setting its CPU or RAM to 0, wake it by giving both back (issue #498).

A hibernated instance is a suspended one (issue #594) with a reason of its own: its VM
is stopped and its disk, row, address and account are kept. What sets it apart is that
nothing but a resize brings it back -- a top-up does not revive it and no grace period
ends it -- and that it is charged for its disk, which is all it holds.

``ModifyServiceSystemResources`` decides by the figures the instance would have *after*
the request: a field the request does not set keeps the value of the row, and a
hibernated row holds 0 for both. So a request that sets only one of the two cannot wake it.
"""

from typing import Optional

from protos import celaut_pb2
from src.database.sql_connection import SQLConnection
from src.manager.manager import _revive_locked, get_sysresources
from src.manager.modify_resources import modify_sysreq
from src.utils import dead_instances
from src.utils import logger as log
from src.utils.instance_lock import instance_lock
from src.virtualizers.interface import kill, revivable

sc = SQLConnection()

RESIZE = "resize"
HIBERNATE = "hibernate"
WAKE = "wake"
STAY = "stay"


class HibernationRefused(Exception):
    """The instance cannot be hibernated, and the message says why."""


def plan(token: str, sysreq: celaut_pb2.Sysresources) -> str:
    """What a ``ModifyServiceSystemResources`` request means for the instance ``token``.

    ``HIBERNATE`` for a running instance that would be left with no CPU or no RAM,
    ``WAKE`` for a hibernated one that would have both, ``STAY`` for a hibernated one
    that would still lack one (nothing changes), and ``RESIZE`` for the rest.
    """
    row = sc.get_sys_req(id=token)
    mem = sysreq.mem_limit if sysreq.HasField("mem_limit") else int(row["mem_limit"] or 0)
    quota = sysreq.cpu_quota if sysreq.HasField("cpu_quota") else int(row["cpu_quota"] or 0)
    has_both = mem > 0 and quota > 0
    if sc.is_local_instance_hibernated(id=token):
        return WAKE if has_both else STAY
    return RESIZE if has_both else HIBERNATE


def check_can_hibernate(token: str) -> None:
    """Raise `HibernationRefused` unless ``token`` can be stopped now and started again.

    Asked before the caller is answered, so a refusal reaches it instead of a reply that
    promised a hibernation. It needs the disk and launch spec a revive reproduces, and it
    refuses an instance with a shared filesystem: its share lives with the VM that
    exports it, and the guests that mount it would lose it.
    """
    if sc.is_local_instance_hibernated(id=token):
        return
    if not revivable(token):
        raise HibernationRefused(
            "This instance cannot be started again from its own disk, so it cannot be hibernated."
        )
    if _uses_shares(token):
        raise HibernationRefused(
            "This instance exports or mounts a shared filesystem, and hibernating it would "
            "take that share away from the guests that use it."
        )


def _uses_shares(token: str) -> bool:
    from src.virtualizers.microvm.runtime_state import load_runtime_state
    from src.virtualizers.microvm.shares import network_share_ids

    state = load_runtime_state(token) or {}
    return bool(state.get("exported_shares") or network_share_ids(state.get("virtiofs") or []))


def hibernate_instance(token: str, reason: str = dead_instances.HIBERNATED) -> bool:
    """Stop the VM of ``token`` and keep its disk, with its row zeroed to what it keeps.

    The VM goes first and the row is marked after, as in `suspend_instance`: a failure in
    between leaves a live row at its old price, which is charged in full and tried again by
    the next request. True if the instance is hibernated when this returns.

    ``reason`` is `hibernated` when a client asked for it. The activity window passes
    `hibernated_by_window` (issue #344): the row then keeps the CPU and RAM it had, and
    `wake_window_instances` gives them back at the next opening.
    """
    with instance_lock(token):
        if not sc.internal_instance_exists(id=token):
            return False
        if sc.is_local_instance_hibernated(id=token):
            return True
        check_can_hibernate(token)
        log.LOGGER(f"Hibernate instance {token}: keeping its disk.")
        kill(vmachine_id=token, archive=False, keep_disk=True)
        sc.hibernate_local_instance(token, reason=reason)
        return True


def wake_instance(token: str, sysreq: celaut_pb2.Sysresources) -> bool:
    """Start a hibernated instance again on its own disk, with the resources of ``sysreq``.

    The same admission as any growth (the service pool and the host limits, in
    `modify_sysreq`) and as a revive. If any of it fails the row is zeroed again and the
    instance stays hibernated, so the caller can be refunded. True if it is running.
    """
    with instance_lock(token):
        if not sc.is_local_instance_hibernated(id=token):
            return False
        wanted = celaut_pb2.Sysresources()
        for field in ("mem_limit", "cpu_period", "cpu_quota"):
            if sysreq.HasField(field):
                setattr(wanted, field, getattr(sysreq, field))
        if not modify_sysreq(id=token, sys_req=wanted):
            return False
        try:
            if _revive_locked(token):
                return True
        except Exception as e:
            log.LOGGER(f"Could not wake {token}: {e}")
        _zero_row(token)
        return False


def wake_window_instances() -> int:
    """Wake every instance that the activity window hibernated (issue #344), with the
    resources it held when it went down. Returns how many are running again.

    Called on each tick while the node is open. One that cannot be woken (the service
    pool or the host limits have no room for it) stays hibernated and is tried again on
    the next tick; one that a client hibernated (`hibernated`) is not in this list and is
    never touched. Never raises: this runs in the manager loop.
    """
    woken = 0
    try:
        rows = sc.get_window_hibernated_instances()
    except Exception as e:
        log.LOGGER(f"Error reading the instances hibernated by the activity window: {e}")
        return 0
    for row in rows:
        token = row["id"]
        saved = {field: row.get(field) for field in ("mem_limit", "cpu_period", "cpu_quota")}
        if not saved["mem_limit"] or not saved["cpu_quota"]:
            log.LOGGER(f"Instance {token} was hibernated by the activity window but has no "
                       "saved resources; it stays hibernated.")
            continue
        try:
            if wake_instance(token, celaut_pb2.Sysresources(**{
                    field: int(value) for field, value in saved.items() if value is not None})):
                woken += 1
                log.LOGGER(f"Instance {token} is running again: the activity window opened.")
            else:
                log.LOGGER(f"Instance {token} could not be woken yet; it stays hibernated.")
        except Exception as e:
            log.LOGGER(f"Error waking {token} at opening time: {e}")
    return woken


def is_growth(token: str, sysreq: celaut_pb2.Sysresources) -> bool:
    """Whether ``sysreq`` asks ``token`` for more of any resource than its row holds.

    A closed node refuses this (issue #344), or an instance could take more of a machine
    that is not taking work. A field the request does not set keeps the row's value, so
    it is never growth.
    """
    row = sc.get_sys_req(id=token)
    return any(
        sysreq.HasField(field) and getattr(sysreq, field) > int(row[field] or 0)
        for field in ("mem_limit", "disk_space", "cpu_period", "cpu_quota")
    )


def _zero_row(token: str) -> None:
    sc.update_sys_req(id=token, mem_limit=0, cpu_period=0, cpu_quota=0)


def hibernated_view(token: str) -> celaut_pb2.ModifyServiceSystemResourcesOutput:
    """The answer to the request that hibernates ``token``, given before it is stopped."""
    row = sc.get_sys_req(id=token)
    return celaut_pb2.ModifyServiceSystemResourcesOutput(
        sysreq=celaut_pb2.Sysresources(
            mem_limit=0, disk_space=int(row["disk_space"] or 0), cpu_period=0, cpu_quota=0,
        ),
        balance=get_sysresources(id=token).balance,
    )
