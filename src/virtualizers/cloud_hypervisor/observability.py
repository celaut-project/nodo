import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psutil

from src.virtualizers.cloud_hypervisor.runtime_state import load_runtime_state


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


def _process_snapshot(pid: int) -> Dict[str, Optional[Any]]:
    if pid <= 0:
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


def get_vm_runtime_snapshot(
    vmachine_id: str,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_state = dict(state or load_runtime_state(vmachine_id) or {})
    pid = int(runtime_state.get("pid") or 0)
    proc = _process_snapshot(pid)

    uptime_s = proc["uptime_s"]
    if uptime_s is None:
        uptime_s = _uptime_from_created_at(str(runtime_state.get("created_at") or ""))

    return {
        "vmachine_id": vmachine_id,
        "pid": pid if pid > 0 else None,
        "alive": bool(proc["alive"]),
        "uptime_s": uptime_s,
        "mem_rss_bytes": proc["mem_rss_bytes"],
        "log_paths": _resolve_log_paths(runtime_state),
    }
