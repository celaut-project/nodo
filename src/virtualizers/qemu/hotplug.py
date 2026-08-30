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
~600MB -> ~194MB), and it only ever surrenders free pages, so it never OOMs a
guest that is actually using its RAM. So memory is driven through the balloon and
the cgroup ``memory.max`` is kept as a *ceiling at the boot allocation*, never as
the shrink knob. CPU is unchanged: cgroup ``cpu.max`` throttles the vCPU threads
correctly for both backends, so that reuses CH's helper directly.

Falls back to CH's cgroup-only behaviour (with an explicit best-effort caveat)
for instances launched before the QMP socket existed.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from protos import celaut_pb2
from src.manager.modify_resources import modify_sysreq
from src.utils import logger as log
from src.virtualizers.ch.cgroups import (
    apply_cpu_limit,
    apply_memory_limit,
    cgroup_v2_available,
    ensure_vm_cgroup,
)
from src.virtualizers.ch.runtime_state import load_runtime_state, save_runtime_state
from src.virtualizers.qemu.qmp import QMPClient, QMPError

# Guests cannot use less than a small floor; a balloon target of zero would ask
# the guest to surrender everything. Keep a conservative floor.
MIN_BALLOON_BYTES = 64 * 1024 * 1024

# Headroom left to the guest on top of what it reports as in use. The guest is
# still running while the balloon inflates -- an allocation between the reading
# and the resize must not be the one that pushes it over. 64 MiB is the same
# order as the floor above and is small next to any realistic service.
BALLOON_SAFETY_MARGIN_BYTES = 64 * 1024 * 1024

# Floor used when the guest cannot say how much it has free. Reclaiming nothing
# is always survivable; guessing is not.
BALLOON_BLIND_FLOOR_BYTES = 256 * 1024 * 1024


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
    less a margin for what it allocates while we are resizing. When the guest
    cannot report (no stats, no driver), reclaim nothing below a conservative
    floor: a resize that under-delivers is a priced-wrong instance, while one
    that over-delivers is a dead one.

    Returns ``(target, note)`` where ``note`` is None when the request was
    honoured as-is, and otherwise explains what was clamped and why.
    """
    requested = max(MIN_BALLOON_BYTES, min(int(requested), int(boot_mem_bytes)))

    free = qmp.guest_free_bytes()
    if free is None:
        floor = min(int(boot_mem_bytes), BALLOON_BLIND_FLOOR_BYTES)
        if requested >= floor:
            return requested, None
        return floor, (
            f"guest does not report balloon statistics, so the memory it is using is "
            f"unknown; held at {floor} bytes instead of the requested {requested} "
            f"rather than risk inflating into the guest's working set"
        )

    # in_use = everything the guest has not told us is free.
    safe_floor = int(boot_mem_bytes) - int(free) + BALLOON_SAFETY_MARGIN_BYTES
    safe_floor = max(MIN_BALLOON_BYTES, min(safe_floor, int(boot_mem_bytes)))

    if requested >= safe_floor:
        return requested, None

    return safe_floor, (
        f"requested {requested} bytes is below what the guest is using "
        f"({int(boot_mem_bytes) - int(free)} bytes in use, {int(free)} free of "
        f"{int(boot_mem_bytes)}); clamped to {safe_floor} bytes "
        f"(+{BALLOON_SAFETY_MARGIN_BYTES} margin) so the guest is not OOM-panicked "
        f"by the resize"
    )


def _apply_memory_balloon(
    *,
    qmp_socket: str,
    target_bytes: int,
    boot_mem_bytes: int,
    vm_cgroup,
    cgroup_path: str,
) -> Dict[str, Any]:
    """Resize guest memory via the balloon, keeping the cgroup a safe ceiling.

    Shrink: inflate the balloon toward ``target`` (guest returns pages), then
    the cgroup cap can stay at the boot allocation -- never shrunk below it, so
    the qemu process is never squeezed into OOM. Grow: deflate the balloon back
    up (bounded by the boot ``-m``; QEMU cannot exceed its boot allocation, so a
    request above it is clamped and reported).

    The shrink is additionally bounded by what the guest reports it can spare;
    see :func:`_safe_balloon_target`. A request below that bound is honoured as
    far as it safely can be and reported as ``clamped``, because the alternative
    -- delivering it exactly -- kills the guest.
    """
    clamped = max(MIN_BALLOON_BYTES, min(int(target_bytes), int(boot_mem_bytes)))
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
            f"virtio-balloon target set to {clamped} bytes via QMP; cgroup "
            f"memory.max held at boot allocation {int(boot_mem_bytes)} in {cgroup_path}"
        )
        if int(target_bytes) > int(boot_mem_bytes):
            detail += (
                f" (requested {int(target_bytes)} exceeds boot -m; clamped -- QEMU "
                f"cannot grow a guest above its boot allocation)"
            )
        if safety_note:
            # Reported as its own status so a caller can tell "you got what you
            # asked for" from "you got as much as was survivable".
            result = _field_result(
                status="clamped",
                detail=f"{detail} ({safety_note})",
                requested=int(target_bytes),
            )
            result["delivered"] = int(clamped)
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
    qmp_socket = str(state.get("qmp_socket") or "")
    boot_mem_bytes = int(state.get("boot_mem_bytes") or 0)

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
            )
        else:
            # Legacy instance without a QMP socket: cgroup-only, and honestly
            # flagged as best-effort because a shrink below the guest's resident
            # set can swap or OOM the qemu process (the guest is not resized).
            try:
                apply_memory_limit(vm_cgroup=vm_cgroup, mem_limit=target)
                report["results"]["mem_limit"] = _field_result(
                    "applied",
                    f"cgroup memory.max set to {target} in {cgroup_path} "
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
