"""Live resource resize for a QEMU (TCG) cross-arch guest.

Why QEMU needs its own hotplug rather than reusing CH's cgroup path:

CH resizes a guest's memory by talking to the guest (its own balloon/mem path),
so the guest genuinely gives RAM back. QEMU here boots with a *fixed* ``-m``
allocation. Enforcing a memory *shrink* purely by tightening the process's
cgroup ``memory.max`` (what ``ch_hotplug`` does) does not resize the guest at
all: the guest still believes it has its full ``-m`` and keeps its resident
pages, so the kernel either swaps the qemu process or -- with no swap --
**OOM-kills it**. Both were reproduced live on nodo#274: shrinking ``memory.max``
below the guest's resident set killed ``qemu-system-aarch64`` outright.

The correct primitive is ``virtio-balloon`` over QMP: inflating the balloon makes
the *guest* return its free pages to the host (host RSS drops, proven live:
~600MB -> ~194MB). It only ever surrenders free pages -- but that bounds which
pages move, not which target is legal, so the target itself must be bounded by
what the guest can spare (:func:`_safe_balloon_target`). So memory is driven
through the balloon and the cgroup ``memory.max`` is kept as a *ceiling at the
boot allocation*, never as the shrink knob. CPU is unchanged: cgroup ``cpu.max``
throttles the vCPU threads correctly for both backends, so that reuses CH's
helper directly.

The boot allocation this is all bounded by is the guest's declared ``at_most``,
not its ``at_init``: ``-m`` cannot be raised on a running QEMU, so a guest booted
at ``at_init`` has no headroom for the grow half of a resize to reach into. The
difference is held by the balloon from the moment the guest comes up
(:func:`settle_boot_balloon`), so a guest still *has* only what it was granted.

Falls back to CH's cgroup-only behaviour (with an explicit best-effort caveat)
for instances launched before the QMP socket existed.
"""
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from protos import celaut_pb2
from src.manager.modify_resources import modify_sysreq
from src.utils import logger as log
from src.virtualizers.microvm.cgroups import (
    apply_cpu_limit,
    apply_memory_limit,
    cgroup_v2_available,
    ensure_vm_cgroup,
)
from src.virtualizers.microvm.runtime_state import load_runtime_state, save_runtime_state
from src.virtualizers.qemu.qmp import QMPClient, QMPError

# Guests cannot use less than a small floor; a balloon target of zero would ask
# the guest to surrender everything. Keep a conservative floor.
MIN_BALLOON_BYTES = 64 * 1024 * 1024

# Headroom left to the guest on top of what it reports as in use. The guest is
# still running while the balloon inflates -- an allocation between the reading
# and the resize must not be the one that pushes it over. 64 MiB is the same
# order as the floor above and is small next to any realistic service.
BALLOON_SAFETY_MARGIN_BYTES = 64 * 1024 * 1024

# How long the boot-time squeeze waits for the guest to hand back the headroom it
# was booted with. The guest driver fulfils a balloon target asynchronously, and
# what it has actually returned is the figure the instance is billed for, so the
# launch waits for the answer instead of assuming one. Generous because this runs
# under TCG, where everything the guest does is an order of magnitude slower.
BOOT_BALLOON_SETTLE_TIMEOUT_S = 30.0
BOOT_BALLOON_POLL_INTERVAL_S = 0.5

# The guest driver works in pages, so `actual` can land a hair off a target that is
# not page-aligned. Close enough is reached, not still-in-progress.
BOOT_BALLOON_TOLERANCE_BYTES = 1024 * 1024


def _field_result(status: str, detail: str, requested: Any = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"status": status, "detail": detail}
    if requested is not None:
        payload["requested"] = requested
    return payload


def _cpu_requested(sysreq: celaut_pb2.Sysresources) -> bool:
    if not sysreq:
        return False
    if not (sysreq.HasField("cpu_period") or sysreq.HasField("cpu_quota")):
        return False
    period = int(sysreq.cpu_period) if sysreq.HasField("cpu_period") else 0
    quota = int(sysreq.cpu_quota) if sysreq.HasField("cpu_quota") else 0
    return (period > 0) or (quota > 0)


def _persist_report(vmachine_id: str, state: Dict[str, Any], report: Dict[str, Any], cgroup_path: str) -> None:
    new_state = dict(state or {})
    new_state["vmachine_id"] = vmachine_id
    new_state["cgroup_path"] = cgroup_path
    new_state["last_hotplug_report"] = report
    save_runtime_state(vmachine_id, new_state)


def _optional_reading(qmp, name: str) -> Optional[int]:
    """A reading the QMP client may not be able to give.

    A client that predates these helpers (or a stub) simply has no such method,
    and QEMU may have nothing to report even when it does. Both answers are the
    same one -- "cannot tell" -- and neither is a failed resize, so neither may
    raise out of here.
    """
    reader = getattr(qmp, name, None)
    if not callable(reader):
        return None
    value = reader()
    return int(value) if value else None


def _safe_balloon_target(qmp, requested: int, boot_mem_bytes: int) -> tuple:
    """The smallest balloon target that does not starve a running guest.

    The balloon's documented safety -- "the guest only surrenders free pages" --
    holds for the *pages*, not for the target. Asking a guest to shrink below
    what it is actually using does not fail politely: the guest driver keeps
    allocating to satisfy the request until its own allocator gives up, and the
    guest kernel panics with "Out of memory and no killable processes". Observed
    exactly that on a live arm64 guest: a caller asked for 64 MiB against a
    954 MiB boot allocation and the guest died mid-request.

    So clamp the request to what the guest says it can spare -- its free memory,
    less a margin for what it allocates while we are resizing -- measured against
    what the guest *currently* has rather than its boot allocation, since the
    balloon may already hold part of the difference.

    When the guest cannot report, reclaim nothing: leave it the memory it already
    has. A guest that cannot publish statistics is typically one with no balloon
    driver, which would not return the pages anyway; and of the two ways to be
    wrong, an under-delivered resize is a mispriced instance while an
    over-delivered one is a dead guest.

    Returns ``(target, note)`` where ``note`` is None when the request was
    honoured as-is, and otherwise explains what was clamped and why.
    """
    requested = max(MIN_BALLOON_BYTES, min(int(requested), int(boot_mem_bytes)))

    # What the guest has right now: the boot allocation less whatever the balloon
    # already holds. Its free-memory figure is relative to this, not to boot -m.
    current = _optional_reading(qmp, "balloon_actual_bytes") or int(boot_mem_bytes)
    current = max(MIN_BALLOON_BYTES, min(current, int(boot_mem_bytes)))

    # A grow is bounded only by the boot -m, already applied above. Nothing below
    # may raise the target: a clamp exists to protect the guest from a shrink,
    # never to hand it memory it did not ask for.
    if requested >= current:
        return requested, None

    free = _optional_reading(qmp, "guest_free_bytes")
    if free is None:
        return current, (
            f"guest does not report balloon statistics, so the memory it is using "
            f"is unknown; held at the {current} bytes it already has rather than "
            f"the requested {requested}, since a guest that cannot report is "
            f"typically one with no balloon driver to reclaim from anyway"
        )

    in_use = max(0, current - int(free))
    safe_floor = max(MIN_BALLOON_BYTES, min(in_use + BALLOON_SAFETY_MARGIN_BYTES, current))

    if requested >= safe_floor:
        return requested, None

    return safe_floor, (
        f"requested {requested} bytes is below what the guest is using "
        f"({in_use} bytes in use, {int(free)} free of {current}); clamped to "
        f"{safe_floor} bytes so the guest is not OOM-panicked by the resize"
    )


def settle_boot_balloon(
    *,
    vmachine_id: str,
    qmp_socket: str,
    boot_mem_bytes: int,
    target_bytes: int,
    timeout_s: float = BOOT_BALLOON_SETTLE_TIMEOUT_S,
) -> int:
    """Take back the headroom a QEMU guest is booted with; return what it kept.

    A QEMU guest is booted with its ``at_most`` allocation rather than its
    ``at_init`` one, because ``-m`` is fixed for the life of the process: a guest
    booted at ``at_init`` can never be grown to the ceiling its manifest declared,
    whatever the balloon or the cgroup is told afterwards (see
    :func:`src.virtualizers.microvm.limits.resolve_boot_mem_bytes`). Reserving the
    ceiling is only half of it -- the guest must not *keep* the difference, which
    it was never granted and its balance was not funded for. So the balloon takes
    it back as soon as the guest is up, leaving the guest holding ``at_init`` and
    the host holding the rest until a hotplug hands it over.

    The squeeze goes through :func:`_safe_balloon_target` like any other shrink,
    for the same reason: a target below what the guest is using OOM-panics it. The
    boot figure is affordable by construction -- ``at_init`` is what the service
    declared it needs to start, and the guest has barely started -- but a target
    that ought to be affordable is not an argument for skipping the clamp that
    proves it is.

    Returns the bytes the guest holds when this is done, which is what the caller
    must price it at. Usually ``target_bytes``; ``boot_mem_bytes`` when the guest
    did not return the headroom (no balloon driver, no statistics, a QMP failure,
    or simply too slow). A guest that keeps memory is billed for it -- the rule a
    clamped hotplug shrink already follows -- rather than being charged for an
    intention nothing verified.
    """
    _id = vmachine_id[:6]
    boot_mem_bytes = int(boot_mem_bytes)
    target = max(MIN_BALLOON_BYTES, min(int(target_bytes), boot_mem_bytes))
    if target >= boot_mem_bytes:
        # No headroom was reserved: the guest was booted with what it was granted.
        return boot_mem_bytes

    held = boot_mem_bytes
    deadline = time.monotonic() + float(timeout_s)
    try:
        with QMPClient(qmp_socket) as qmp:
            while True:
                safe, note = _safe_balloon_target(qmp, target, boot_mem_bytes)
                if safe < held:
                    qmp.set_balloon(safe)
                time.sleep(BOOT_BALLOON_POLL_INTERVAL_S)

                reading = _optional_reading(qmp, "balloon_actual_bytes")
                held = min(int(reading), boot_mem_bytes) if reading else boot_mem_bytes
                if held <= safe + BOOT_BALLOON_TOLERANCE_BYTES:
                    # Reached what was asked for -- which is the boot allocation
                    # itself when the clamp refused to reclaim anything.
                    if note:
                        log.LOGGER(f"[QEMU][{_id}] boot balloon: {note}")
                    break
                if time.monotonic() >= deadline:
                    log.LOGGER(
                        f"[QEMU][{_id}] boot balloon: guest still holds {held} bytes of its "
                        f"{boot_mem_bytes} boot allocation after {timeout_s}s; billing what it holds"
                    )
                    break
    except Exception as e:
        log.LOGGER(
            f"[QEMU][{_id}] boot balloon failed ({type(e).__name__}: {e}); the guest keeps its "
            f"{boot_mem_bytes} byte boot allocation and is billed for it"
        )
        return boot_mem_bytes

    log.LOGGER(
        f"[QEMU][{_id}] boot balloon settled: guest holds {held} bytes of a {boot_mem_bytes} "
        f"byte boot allocation (target {target})"
    )
    return held


def _apply_memory_balloon(
    *,
    qmp_socket: str,
    target_bytes: int,
    boot_mem_bytes: int,
    vm_cgroup,
    cgroup_path: str,
    reserve_bytes: int = 0,
) -> Dict[str, Any]:
    """Resize guest memory via the balloon, keeping the cgroup a safe ceiling.

    Shrink: inflate the balloon toward ``target`` (guest returns pages), then
    the cgroup cap can stay at the boot allocation -- never shrunk below it, so
    the qemu process is never squeezed into OOM. Grow: deflate the balloon back
    up (bounded by the boot ``-m``; QEMU cannot exceed its boot allocation, so a
    request above it is clamped and reported).

    ``target_bytes`` is what the *service* is to be left able to use, which is not
    what the balloon speaks: a balloon target is a guest allocation, and the guest
    kernel's own footprint comes out of it first. ``reserve_bytes`` is that
    footprint, measured for this guest at boot and carried in its runtime state, so
    a grow to a declared ceiling really leaves the service that much to allocate
    instead of that much minus a kernel. Everything reported back is in usable
    bytes, the unit the request arrived in and the unit the row is priced in.

    The shrink is additionally bounded by what the guest reports it can spare;
    see :func:`_safe_balloon_target`. A request below that bound is honoured as
    far as it safely can be and reported as ``clamped``, because the alternative
    -- delivering it exactly -- kills the guest.
    """
    reserve_bytes = max(0, int(reserve_bytes))
    allocation = int(target_bytes) + reserve_bytes
    clamped = max(MIN_BALLOON_BYTES, min(allocation, int(boot_mem_bytes)))
    safety_note = None
    try:
        with QMPClient(qmp_socket) as qmp:
            clamped, safety_note = _safe_balloon_target(qmp, clamped, boot_mem_bytes)
            qmp.set_balloon(clamped)
        # Keep memory.max pinned at the boot allocation: it is a hard ceiling,
        # not the resize knob. Shrinking it below boot alloc is exactly what OOMs
        # QEMU, so we never do that here.
        apply_memory_limit(vm_cgroup=vm_cgroup, mem_limit=int(boot_mem_bytes))
        detail = (
            f"virtio-balloon target set to {clamped} bytes via QMP "
            f"({int(target_bytes)} usable + {reserve_bytes} guest kernel reserve); "
            f"cgroup memory.max held at boot allocation {int(boot_mem_bytes)} in {cgroup_path}"
        )
        if allocation > int(boot_mem_bytes):
            detail += (
                f" (requested {int(target_bytes)} usable needs {allocation} and exceeds "
                f"boot -m; clamped -- QEMU cannot grow a guest above its boot allocation)"
            )
        if safety_note:
            # Reported as its own status so a caller can tell "you got what you
            # asked for" from "you got as much as was survivable".
            result = _field_result(
                status="clamped",
                detail=f"{detail} ({safety_note})",
                requested=int(target_bytes),
            )
            result["delivered"] = max(0, int(clamped) - reserve_bytes)
            return result
        return _field_result(status="applied", detail=detail, requested=int(target_bytes))
    except QMPError as e:
        return _field_result(status="failed", detail=f"QMP balloon failed: {e}", requested=int(target_bytes))
    except Exception as e:
        return _field_result(status="failed", detail=f"balloon resize error: {e}", requested=int(target_bytes))


def hotplug(
    vmachine_id: str,
    system_requeriments_range: celaut_pb2.ModifyServiceSystemResourcesInput,
) -> bool:
    _id = vmachine_id[:6]
    log.LOGGER(f"[QEMU][{_id}] event=hotplug request")

    if not system_requeriments_range:
        log.LOGGER(f"[QEMU][{_id}] hotplug failed: empty ModifyServiceSystemResourcesInput")
        return False
    sysreq = system_requeriments_range.max_sysreq
    if not sysreq:
        log.LOGGER(f"[QEMU][{_id}] hotplug failed: empty max_sysreq")
        return False

    state = load_runtime_state(vmachine_id) or {}
    pid = int(state.get("pid") or 0)
    # For QEMU the family's `control_socket` *is* the QMP socket: the same socket
    # whose disappearance means the emulator is gone is the one the balloon resize
    # below is issued over.
    qmp_socket = str(state.get("control_socket") or "")
    boot_mem_bytes = int(state.get("boot_mem_bytes") or 0)
    # Measured for this guest at boot and persisted, not re-derived: it is what the
    # kernel actually took, and an operator editing the reserve mid-life must not
    # change the arithmetic for guests already running under the old figure. Absent
    # on an instance launched before the reserve existed, which booted at exactly
    # its usable figure and so has none.
    reserve_bytes = max(0, int(state.get("guest_kernel_reserve_bytes") or 0))

    report: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "vmachine_id": vmachine_id,
        "backend": "qemu",
        "results": {},
        "cgroup_v2_available": cgroup_v2_available(),
    }
    for name, present in (("blkio_weight", sysreq.HasField("blkio_weight")), ("disk_space", sysreq.HasField("disk_space"))):
        val = int(getattr(sysreq, name)) if present else 0
        if present and val > 0:
            report["results"][name] = _field_result("unsupported", f"QEMU hotplug does not support {name}.", val)
        else:
            report["results"][name] = _field_result("ignored", f"{name} not requested.")

    mem_requested = sysreq.HasField("mem_limit")
    cpu_requested = _cpu_requested(sysreq)
    supported_requested = mem_requested or cpu_requested

    if supported_requested and pid <= 0:
        report["results"]["runtime"] = _field_result("failed", "Runtime state has no valid PID.", pid)
        report["success"] = False
        _persist_report(vmachine_id, state, report, str(state.get("cgroup_path", "")))
        log.LOGGER(f"[QEMU][{_id}] hotplug failed: invalid runtime PID ({pid})")
        return False

    cgroup_path = str(state.get("cgroup_path") or "")
    vm_cgroup = None
    if supported_requested:
        try:
            vm_cgroup = ensure_vm_cgroup(vmachine_id=vmachine_id, pid=pid)
            cgroup_path = str(vm_cgroup)
            report["results"]["runtime"] = _field_result("applied", f"VM process validated in cgroup: {cgroup_path}")
        except Exception as e:
            report["results"]["runtime"] = _field_result("failed", str(e))
            report["success"] = False
            _persist_report(vmachine_id, state, report, cgroup_path)
            log.LOGGER(f"[QEMU][{_id}] hotplug failed ensuring cgroup: {e}")
            return False

    # ---- memory: virtio-balloon (falls back to cgroup-only, best-effort) ----
    if mem_requested:
        target = int(sysreq.mem_limit)
        if qmp_socket and boot_mem_bytes > 0:
            report["results"]["mem_limit"] = _apply_memory_balloon(
                qmp_socket=qmp_socket, target_bytes=target, boot_mem_bytes=boot_mem_bytes,
                vm_cgroup=vm_cgroup, cgroup_path=cgroup_path,
                reserve_bytes=reserve_bytes,
            )
        else:
            # Legacy instance without a QMP socket: cgroup-only, and honestly
            # flagged as best-effort because a shrink below the guest's resident
            # set can swap or OOM the qemu process (the guest is not resized).
            #
            # The cgroup bounds the qemu *process*, so it is set to the guest
            # allocation the target implies, reserve included -- capping it at the
            # usable figure would have the host kill the VM for holding RAM the node
            # itself handed it at boot.
            allocation = target + reserve_bytes
            try:
                apply_memory_limit(vm_cgroup=vm_cgroup, mem_limit=allocation)
                report["results"]["mem_limit"] = _field_result(
                    "applied",
                    f"cgroup memory.max set to {allocation} in {cgroup_path} "
                    f"({target} usable + {reserve_bytes} guest kernel reserve) "
                    f"(best-effort: no QMP balloon; a shrink below the guest RSS may swap/OOM qemu)",
                    target,
                )
            except Exception as e:
                report["results"]["mem_limit"] = _field_result("failed", str(e), target)
    else:
        report["results"]["mem_limit"] = _field_result("ignored", "mem_limit not requested.")

    # ---- cpu: cgroup cpu.max (identical to CH; correct for both backends) ----
    if cpu_requested:
        period = int(sysreq.cpu_period) if sysreq.HasField("cpu_period") else 0
        quota = int(sysreq.cpu_quota) if sysreq.HasField("cpu_quota") else 0
        if period <= 0:
            report["results"]["cpu"] = _field_result("failed", "cpu_period must be > 0.", {"cpu_quota": quota, "cpu_period": period})
        else:
            try:
                apply_cpu_limit(vm_cgroup=vm_cgroup, cpu_quota=quota, cpu_period=period)
                report["results"]["cpu"] = _field_result("applied", f"Applied cpu.max in {cgroup_path}", {"cpu_quota": quota, "cpu_period": period})
            except Exception as e:
                report["results"]["cpu"] = _field_result("failed", str(e), {"cpu_quota": quota, "cpu_period": period})
    else:
        report["results"]["cpu"] = _field_result("ignored", "cpu_period/cpu_quota not requested.")

    strict_ok = True
    if mem_requested and report["results"]["mem_limit"]["status"] not in ("applied", "clamped"):
        strict_ok = False
    if cpu_requested and report["results"]["cpu"]["status"] != "applied":
        strict_ok = False

    # A clamped shrink is a real resize, just not the requested one, so the
    # instance must be priced at what it actually holds. Recording the request
    # instead would bill a guest for less memory than it still has.
    if report["results"].get("mem_limit", {}).get("status") == "clamped":
        delivered = report["results"]["mem_limit"].get("delivered")
        if delivered:
            sysreq.mem_limit = int(delivered)

    if strict_ok:
        if not modify_sysreq(id=vmachine_id, sys_req=sysreq):
            report["results"]["db"] = _field_result("failed", "modify_sysreq rejected DB update.")
            strict_ok = False
        else:
            report["results"]["db"] = _field_result("applied", "System requirements persisted in DB.")
    else:
        report["results"]["db"] = _field_result("ignored", "DB update skipped because a supported field failed.")

    report["success"] = strict_ok
    _persist_report(vmachine_id, state, report, cgroup_path)
    log.LOGGER(
        f"[QEMU][{_id}] event=hotplug result={strict_ok}, "
        f"mem={report['results']['mem_limit']['status']}, cpu={report['results']['cpu']['status']}"
    )
    return strict_ok
