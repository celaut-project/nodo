import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import psutil

from src.virtualizers.ch.cgroups import CGROUPS_BASE_DIR
from src.virtualizers.ch.process import pid_alive
from src.virtualizers.ch.runtime_state import load_runtime_state


def _parse_iso8601(ts: str) -> Optional[datetime]:
    text = str(ts or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _uptime_from_created_at(created_at: str) -> Optional[int]:
    dt = _parse_iso8601(created_at)
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    delta_s = int((now - dt).total_seconds())
    return max(0, delta_s)


def _process_snapshot(pid: int, vmachine_id: str = "") -> Dict[str, Optional[Any]]:
    if pid <= 0:
        return {"alive": False, "uptime_s": None, "mem_rss_bytes": None}
    if vmachine_id and not pid_alive(pid=pid, vmachine_id=vmachine_id):
        return {"alive": False, "uptime_s": None, "mem_rss_bytes": None}

    try:
        proc = psutil.Process(pid)
        alive = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        if not alive:
            return {"alive": False, "uptime_s": None, "mem_rss_bytes": None}

        create_time = proc.create_time()
        uptime_s = max(0, int(time.time() - create_time))
        mem_rss_bytes = int(proc.memory_info().rss)
        return {
            "alive": True,
            "uptime_s": uptime_s,
            "mem_rss_bytes": mem_rss_bytes,
        }
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return {"alive": False, "uptime_s": None, "mem_rss_bytes": None}
    except Exception:
        return {"alive": False, "uptime_s": None, "mem_rss_bytes": None}


def _resolve_log_paths(state: Dict[str, Any]) -> Dict[str, str]:
    candidates = {
        "stdout": state.get("stdout_log"),
        "stderr": state.get("stderr_log"),
        "serial": state.get("serial_log"),
    }
    return {
        key: str(value).strip()
        for key, value in candidates.items()
        if isinstance(value, str) and str(value).strip()
    }


def _guess_cgroup_path(vmachine_id: str, runtime_state: Dict[str, Any]) -> Optional[Path]:
    configured = str(runtime_state.get("cgroup_path") or "").strip()
    if configured:
        return Path(configured)

    safe_id = str(vmachine_id or "").strip()
    if not safe_id or "/" in safe_id:
        return None
    return Path(CGROUPS_BASE_DIR) / "nodo-ch" / safe_id


def _read_first_line(path: Path) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.readline().strip()
        return raw or None
    except Exception:
        return None


def _parse_cgroup_int(raw: Optional[str]) -> Optional[int]:
    text = str(raw or "").strip()
    if not text or text == "max":
        return None
    try:
        return int(text)
    except Exception:
        return None


def _cgroup_memory_snapshot(
    vmachine_id: str,
    runtime_state: Dict[str, Any],
) -> Dict[str, Any]:
    cgroup_path = _guess_cgroup_path(vmachine_id=vmachine_id, runtime_state=runtime_state)
    if not cgroup_path or not cgroup_path.exists():
        return {
            "path": str(cgroup_path) if cgroup_path else None,
            "memory_max_raw": None,
            "memory_max_bytes": None,
            "memory_current_bytes": None,
        }

    memory_max_raw = _read_first_line(cgroup_path / "memory.max")
    memory_current_raw = _read_first_line(cgroup_path / "memory.current")
    return {
        "path": str(cgroup_path),
        "memory_max_raw": memory_max_raw,
        "memory_max_bytes": _parse_cgroup_int(memory_max_raw),
        "memory_current_bytes": _parse_cgroup_int(memory_current_raw),
    }


def _cgroup_io_snapshot(cgroup_path: Optional[Path]) -> Dict[str, Optional[int]]:
    """Cumulative block-IO from cgroup v2 ``io.stat`` (summed over devices).

    Each line is ``MAJ:MIN key=val key=val ...`` with ``rbytes``/``wbytes``
    (bytes read/written to backing storage). Returns ``None`` totals when the
    file is absent (io controller not enabled) so callers keep unset-on-the-wire.
    """
    if not cgroup_path:
        return {"disk_read_bytes": None, "disk_write_bytes": None}
    read_total = 0
    write_total = 0
    saw_any = False
    try:
        with open(cgroup_path / "io.stat", "r", encoding="utf-8") as f:
            for line in f:
                for field in line.split()[1:]:
                    if "=" not in field:
                        continue
                    key, _, value = field.partition("=")
                    try:
                        num = int(value)
                    except ValueError:
                        continue
                    if key == "rbytes":
                        read_total += num
                        saw_any = True
                    elif key == "wbytes":
                        write_total += num
                        saw_any = True
    except Exception:
        return {"disk_read_bytes": None, "disk_write_bytes": None}
    if not saw_any:
        return {"disk_read_bytes": None, "disk_write_bytes": None}
    return {"disk_read_bytes": read_total, "disk_write_bytes": write_total}


def _process_io_snapshot(pid: int) -> Dict[str, Optional[int]]:
    """Per-VM disk I/O from the hypervisor process's ``/proc/<pid>/io``.

    Fallback for disk usage when the cgroup ``io`` controller is not delegated
    to the instance's leaf cgroup (the common case: the leaf only carries
    ``cpu``/``memory``, so ``io.stat`` lives only on the parent and would mix
    instances). ``read_bytes``/``write_bytes`` are the bytes actually fetched
    from / sent to the storage layer by this VM's process.
    """
    if pid <= 0:
        return {"disk_read_bytes": None, "disk_write_bytes": None}
    try:
        io = psutil.Process(pid).io_counters()
        return {"disk_read_bytes": int(io.read_bytes),
                "disk_write_bytes": int(io.write_bytes)}
    except Exception:
        return {"disk_read_bytes": None, "disk_write_bytes": None}


def _tap_ifname(vmachine_id: str) -> Optional[str]:
    """Host tap interface name for ``vmachine_id``.

    Mirrors ``execute.py::_create_tap`` / ``observe.tap_ifname_for_instance``:
    ``tap`` + first 10 hex chars of ``sha1(instance_id)``. Re-derived (not
    stored) so this stays read-only and never diverges from the runtime value.
    """
    import hashlib

    safe_id = str(vmachine_id or "").strip()
    if not safe_id:
        return None
    return "tap" + hashlib.sha1(safe_id.encode("utf-8")).hexdigest()[:10]


def _read_net_counter(ifname: str, name: str) -> Optional[int]:
    try:
        with open(f"/sys/class/net/{ifname}/statistics/{name}",
                  "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _tap_net_snapshot(vmachine_id: str) -> Dict[str, Optional[int]]:
    """Cumulative tap-interface byte/packet counters.

    Read from ``/sys/class/net/<tap>/statistics``. Orientation is the host tap's:
    ``rx`` = frames the host received from the VM (VM egress); ``tx`` = frames the
    host sent to the VM (VM ingress). ``None`` when the tap is absent.
    """
    ifname = _tap_ifname(vmachine_id)
    if not ifname:
        return {"net_rx_bytes": None, "net_tx_bytes": None,
                "net_rx_packets": None, "net_tx_packets": None}
    return {
        "net_rx_bytes": _read_net_counter(ifname, "rx_bytes"),
        "net_tx_bytes": _read_net_counter(ifname, "tx_bytes"),
        "net_rx_packets": _read_net_counter(ifname, "rx_packets"),
        "net_tx_packets": _read_net_counter(ifname, "tx_packets"),
    }


def get_vm_runtime_snapshot(
    vmachine_id: str,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_state = dict(state or load_runtime_state(vmachine_id) or {})
    pid = int(runtime_state.get("pid") or 0)
    proc = _process_snapshot(pid, vmachine_id=vmachine_id)
    cgroup_memory = _cgroup_memory_snapshot(vmachine_id=vmachine_id, runtime_state=runtime_state)
    cgroup_io = _cgroup_io_snapshot(
        _guess_cgroup_path(vmachine_id=vmachine_id, runtime_state=runtime_state))
    proc_io = _process_io_snapshot(pid)
    disk_read_bytes = (cgroup_io["disk_read_bytes"]
                       if cgroup_io["disk_read_bytes"] is not None
                       else proc_io["disk_read_bytes"])
    disk_write_bytes = (cgroup_io["disk_write_bytes"]
                        if cgroup_io["disk_write_bytes"] is not None
                        else proc_io["disk_write_bytes"])
    net_io = _tap_net_snapshot(vmachine_id=vmachine_id)

    uptime_s = proc["uptime_s"]
    if uptime_s is None:
        uptime_s = _uptime_from_created_at(str(runtime_state.get("created_at") or ""))

    return {
        "vmachine_id": vmachine_id,
        "pid": pid if pid > 0 else None,
        "alive": bool(proc["alive"]),
        "uptime_s": uptime_s,
        "mem_rss_bytes": proc["mem_rss_bytes"],
        "cgroup_path": cgroup_memory["path"],
        "cgroup_memory_max_raw": cgroup_memory["memory_max_raw"],
        "cgroup_memory_max_bytes": cgroup_memory["memory_max_bytes"],
        "cgroup_memory_current_bytes": cgroup_memory["memory_current_bytes"],
        "disk_read_bytes": disk_read_bytes,
        "disk_write_bytes": disk_write_bytes,
        "net_rx_bytes": net_io["net_rx_bytes"],
        "net_tx_bytes": net_io["net_tx_bytes"],
        "net_rx_packets": net_io["net_rx_packets"],
        "net_tx_packets": net_io["net_tx_packets"],
        "log_paths": _resolve_log_paths(runtime_state),
    }
