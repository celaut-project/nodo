from datetime import datetime, timezone
from typing import Any, Dict

from protos import celaut_pb2
from src.manager.modify_resources import modify_sysreq
from src.utils import logger as log
from src.virtualizers.cloud_hypervisor.cgroups import (
    apply_cpu_limit,
    apply_memory_limit,
    cgroup_v2_available,
    ensure_vm_cgroup,
)
from src.virtualizers.cloud_hypervisor.runtime_state import (
    load_runtime_state,
    save_runtime_state,
)


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


def _persist_report(
    vmachine_id: str,
    state: Dict[str, Any],
    report: Dict[str, Any],
    cgroup_path: str,
) -> None:
    new_state = dict(state or {})
    new_state["vmachine_id"] = vmachine_id
    new_state["cgroup_path"] = cgroup_path
    new_state["last_hotplug_report"] = report
    save_runtime_state(vmachine_id, new_state)


def hotplug(
    vmachine_id: str,
    system_requeriments_range: celaut_pb2.ModifyServiceSystemResourcesInput,
) -> bool:
    _id = vmachine_id[:6]
    log.LOGGER(f"[CH][{_id}] hotplug request received")

    if not system_requeriments_range:
        log.LOGGER(f"[CH][{_id}] hotplug failed: empty ModifyServiceSystemResourcesInput")
        return False

    sysreq = system_requeriments_range.max_sysreq
    if not sysreq:
        log.LOGGER(f"[CH][{_id}] hotplug failed: empty max_sysreq")
        return False

    state = load_runtime_state(vmachine_id) or {}
    pid = int(state.get("pid") or 0)

    report: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "vmachine_id": vmachine_id,
        "results": {},
        "cgroup_v2_available": cgroup_v2_available(),
    }

    # Unsupported fields: explicit reporting, permissive behavior.
    if sysreq.HasField("blkio_weight"):
        if int(sysreq.blkio_weight) > 0:
            report["results"]["blkio_weight"] = _field_result(
                status="unsupported",
                detail="Cloud Hypervisor hotplug phase 4 does not support blkio_weight.",
                requested=int(sysreq.blkio_weight),
            )
        else:
            report["results"]["blkio_weight"] = _field_result(
                status="ignored",
                detail="blkio_weight=0 treated as unspecified.",
                requested=int(sysreq.blkio_weight),
            )
    else:
        report["results"]["blkio_weight"] = _field_result(
            status="ignored",
            detail="blkio_weight not requested.",
        )

    if sysreq.HasField("disk_space"):
        if int(sysreq.disk_space) > 0:
            report["results"]["disk_space"] = _field_result(
                status="unsupported",
                detail="Cloud Hypervisor hotplug phase 4 does not support disk_space.",
                requested=int(sysreq.disk_space),
            )
        else:
            report["results"]["disk_space"] = _field_result(
                status="ignored",
                detail="disk_space=0 treated as unspecified.",
                requested=int(sysreq.disk_space),
            )
    else:
        report["results"]["disk_space"] = _field_result(
            status="ignored",
            detail="disk_space not requested.",
        )

    mem_requested = sysreq.HasField("mem_limit")
    cpu_requested = _cpu_requested(sysreq)
    supported_requested = mem_requested or cpu_requested

    if supported_requested and pid <= 0:
        report["results"]["runtime"] = _field_result(
            status="failed",
            detail="Runtime state has no valid PID for cgroup update.",
            requested=pid,
        )
        report["success"] = False
        _persist_report(vmachine_id=vmachine_id, state=state, report=report, cgroup_path=str(state.get("cgroup_path", "")))
        log.LOGGER(f"[CH][{_id}] hotplug failed: invalid runtime PID ({pid})")
        return False

    cgroup_path = str(state.get("cgroup_path") or "")
    vm_cgroup = None
    if supported_requested:
        try:
            vm_cgroup = ensure_vm_cgroup(vmachine_id=vmachine_id, pid=pid)
            cgroup_path = str(vm_cgroup)
            report["results"]["runtime"] = _field_result(
                status="applied",
                detail=f"VM process moved/validated in dedicated cgroup: {cgroup_path}",
            )
        except Exception as e:
            report["results"]["runtime"] = _field_result(
                status="failed",
                detail=str(e),
            )
            report["success"] = False
            _persist_report(vmachine_id=vmachine_id, state=state, report=report, cgroup_path=cgroup_path)
            log.LOGGER(f"[CH][{_id}] hotplug failed ensuring cgroup: {e}")
            return False

    if mem_requested:
        try:
            assert vm_cgroup is not None  # guarded by supported_requested
            apply_memory_limit(vm_cgroup=vm_cgroup, mem_limit=int(sysreq.mem_limit))
            report["results"]["mem_limit"] = _field_result(
                status="applied",
                detail=f"Applied memory.max in {cgroup_path}",
                requested=int(sysreq.mem_limit),
            )
        except Exception as e:
            report["results"]["mem_limit"] = _field_result(
                status="failed",
                detail=str(e),
                requested=int(sysreq.mem_limit),
            )
    else:
        report["results"]["mem_limit"] = _field_result(
            status="ignored",
            detail="mem_limit not requested.",
        )

    if cpu_requested:
        period = int(sysreq.cpu_period) if sysreq.HasField("cpu_period") else 0
        quota = int(sysreq.cpu_quota) if sysreq.HasField("cpu_quota") else 0
        if period <= 0:
            report["results"]["cpu"] = _field_result(
                status="failed",
                detail="cpu_period must be > 0 when CPU limits are requested.",
                requested={"cpu_quota": quota, "cpu_period": period},
            )
        else:
            try:
                assert vm_cgroup is not None  # guarded by supported_requested
                apply_cpu_limit(vm_cgroup=vm_cgroup, cpu_quota=quota, cpu_period=period)
                report["results"]["cpu"] = _field_result(
                    status="applied",
                    detail=f"Applied cpu.max in {cgroup_path}",
                    requested={"cpu_quota": quota, "cpu_period": period},
                )
            except Exception as e:
                report["results"]["cpu"] = _field_result(
                    status="failed",
                    detail=str(e),
                    requested={"cpu_quota": quota, "cpu_period": period},
                )
    else:
        report["results"]["cpu"] = _field_result(
            status="ignored",
            detail="cpu_period/cpu_quota not requested.",
        )

    # Strict enforcement for supported fields.
    strict_ok = True
    if mem_requested and report["results"]["mem_limit"]["status"] != "applied":
        strict_ok = False
    if cpu_requested and report["results"]["cpu"]["status"] != "applied":
        strict_ok = False

    if strict_ok:
        if not modify_sysreq(id=vmachine_id, sys_req=sysreq):
            report["results"]["db"] = _field_result(
                status="failed",
                detail="modify_sysreq rejected DB update for system requirements.",
            )
            strict_ok = False
        else:
            report["results"]["db"] = _field_result(
                status="applied",
                detail="System requirements persisted in DB.",
            )
    else:
        report["results"]["db"] = _field_result(
            status="ignored",
            detail="DB update skipped because supported field application failed.",
        )

    report["success"] = strict_ok
    _persist_report(vmachine_id=vmachine_id, state=state, report=report, cgroup_path=cgroup_path)

    if not strict_ok:
        failed_details = {
            name: result
            for name, result in report["results"].items()
            if result.get("status") == "failed"
        }
        if failed_details:
            log.LOGGER(f"[CH][{_id}] hotplug failure details: {failed_details}")

    log.LOGGER(
        f"[CH][{_id}] hotplug result success={strict_ok}, "
        f"mem={report['results']['mem_limit']['status']}, cpu={report['results']['cpu']['status']}, "
        f"blkio={report['results']['blkio_weight']['status']}, disk={report['results']['disk_space']['status']}"
    )
    return strict_ok
