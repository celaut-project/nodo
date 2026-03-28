import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import psutil

from src.virtualizers.ch.cgroups import CGROUPS_BASE_DIR
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


def get_vm_runtime_snapshot(
    vmachine_id: str,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_state = dict(state or load_runtime_state(vmachine_id) or {})
    pid = int(runtime_state.get("pid") or 0)
    proc = _process_snapshot(pid)
    cgroup_memory = _cgroup_memory_snapshot(vmachine_id=vmachine_id, runtime_state=runtime_state)

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
        "log_paths": _resolve_log_paths(runtime_state),
    }
