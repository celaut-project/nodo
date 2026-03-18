import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

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
