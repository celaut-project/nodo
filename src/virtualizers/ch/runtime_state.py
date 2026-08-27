import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.config import ConfigManager

env_manager = ConfigManager()
CACHE = env_manager.get("CACHE")

_LOCK_GUARD = threading.Lock()
_FILE_LOCKS: Dict[str, threading.Lock] = {}


def _runtime_dir() -> Path:
    if not CACHE:
        raise RuntimeError("CACHE path is not configured.")
    return Path(CACHE) / "cloud_hypervisor" / "runtime"


def _state_path(vmachine_id: str) -> Path:
    return _runtime_dir() / f"{vmachine_id}.json"


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCK_GUARD:
        if key not in _FILE_LOCKS:
            _FILE_LOCKS[key] = threading.Lock()
        return _FILE_LOCKS[key]


def save_runtime_state(vmachine_id: str, payload: Dict[str, Any]) -> None:
    path = _state_path(vmachine_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_for(path)

    with lock:
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        tmp_path.replace(path)


def save_booting_state(
    vmachine_id: str,
    *,
    virtualizer: str,
    service_id: str,
    pid: int,
    ip: str,
    mac: str,
    tap: str,
    bridge: str,
    cleanup_rules: List[Any],
    rule_comment_prefix: str,
) -> None:
    """Record a VM the instant its hypervisor process exists, before it is ready.

    The full state is written at the end of ``execute``, once the guest answers on
    the network -- seconds later, and after the guest has already had time to call
    the node. Two readers cannot wait that long:

    * the maintenance sweep prunes any instance in the database that has no runtime
      state (``unhealthy reason=runtime_state_missing``), so an instance recorded
      before it finishes booting needs its state file from the start, or the sweep
      would destroy it mid-boot;
    * the janitor kills any runtime state with no database row, so the two records
      belong to the same moment -- this one is written first and exempted from that
      rule while ``booting`` is set (see ``janitor_cleanup_orphans``).

    ``api_socket`` is deliberately absent: the hypervisor creates that socket a
    moment after it starts, and ``maintain`` reads a recorded-but-missing socket as
    a dead VM. The final write adds it, once it is there to be found.
    """
    save_runtime_state(
        vmachine_id,
        {
            "vmachine_id": vmachine_id,
            "virtualizer": virtualizer,
            "service_id": service_id,
            "pid": pid,
            "ip": ip,
            "mac": mac,
            "tap": tap,
            "bridge": bridge,
            "cleanup_rules": cleanup_rules,
            "rule_comment_prefix": rule_comment_prefix,
            "booting": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def load_runtime_state(vmachine_id: str) -> Optional[Dict[str, Any]]:
    path = _state_path(vmachine_id)
    if not path.is_file():
        return None

    lock = _lock_for(path)
    with lock:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def delete_runtime_state(vmachine_id: str) -> None:
    path = _state_path(vmachine_id)
    if not path.exists():
        return

    lock = _lock_for(path)
    with lock:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def list_runtime_states() -> Dict[str, Dict[str, Any]]:
    runtime_dir = _runtime_dir()
    if not runtime_dir.exists():
        return {}

    states: Dict[str, Dict[str, Any]] = {}
    for path in runtime_dir.glob("*.json"):
        lock = _lock_for(path)
        with lock:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                vmachine_id = data.get("vmachine_id") or path.stem
                states[vmachine_id] = data
            except Exception:
                continue
    return states
