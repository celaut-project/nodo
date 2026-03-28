import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from src.utils import logger as log
from src.utils.config import ConfigManager
from src.virtualizers.ch.cgroups import remove_vm_cgroup
from src.virtualizers.ch.runtime_state import (
    delete_runtime_state,
    load_runtime_state,
)

env_manager = ConfigManager()
CACHE = env_manager.get("CACHE")


def _runtime_dir(vmachine_id: str) -> Optional[Path]:
    if not CACHE:
        return None
    return Path(CACHE) / "cloud_hypervisor" / "runtime" / vmachine_id

# TODO CHExecuteError and _run is duplicated in kill.py and execute.py, consider refactoring to a common utils module.
class CHExecuteError(RuntimeError):
    pass

def _run(command: List[str], *, check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            check=check,
            capture_output=capture_output,
            text=True,
        )
    except FileNotFoundError as e:
        raise CHExecuteError(f"Required command not found: {command[0]}") from e
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else ""
        stdout = e.stdout.strip() if e.stdout else ""
        details: List[str] = []
        if stdout:
            details.append(f"stdout={stdout}")
        if stderr:
            details.append(f"stderr={stderr}")
        raise CHExecuteError(
            f"Command failed ({e.returncode}): {' '.join(command)} -> "
            f"{' | '.join(details) if details else 'unknown error'}"
        ) from e


def _cleanup_dnat_rules(vmachine_id: str, cleanup_rules: List[List[str]]) -> None:
    for rule in cleanup_rules:
        try:
            _run(rule)
            log.LOGGER(f"[CH][{vmachine_id}] cleanup DNAT rule attempted: {rule}")
        except Exception as e:
            log.LOGGER(f"[CH][{vmachine_id}] error cleaning DNAT rule {rule}: {e}")


def _cleanup_tap(vmachine_id: str, tap_name: Optional[str]) -> None:
    if not tap_name:
        return
    _run(["ip", "link", "del", str(tap_name)])
    log.LOGGER(f"[CH][{vmachine_id}] cleanup tap attempted: {tap_name}")


def _kill_pid(vmachine_id: str, pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGKILL)
        log.LOGGER(f"[CH][{vmachine_id}] SIGKILL sent to pid={pid}")
    except ProcessLookupError:
        log.LOGGER(f"[CH][{vmachine_id}] pid already dead: {pid}")
    except PermissionError as e:
        log.LOGGER(f"[CH][{vmachine_id}] permission denied killing pid={pid}: {e}")
    except Exception as e:
        log.LOGGER(f"[CH][{vmachine_id}] failed to kill pid={pid}: {e}")


def kill(vmachine_id: str) -> bool:
    state = load_runtime_state(vmachine_id) or {}
    pid = int(state.get("pid") or 0)
    tap_name = str(state.get("tap") or "")
    cleanup_rules = state.get("cleanup_rules") or []
    cgroup_path = str(state.get("cgroup_path") or "")
    runtime_dir = _runtime_dir(vmachine_id)

    log.LOGGER(f"[CH][{vmachine_id}] event=kill requested")
    _kill_pid(vmachine_id=vmachine_id, pid=pid)
    _cleanup_dnat_rules(vmachine_id=vmachine_id, cleanup_rules=cleanup_rules)
    _cleanup_tap(vmachine_id=vmachine_id, tap_name=tap_name)
    remove_vm_cgroup(vmachine_id=vmachine_id, cgroup_path=cgroup_path)

    try:
        if runtime_dir and runtime_dir.exists():
            shutil.rmtree(runtime_dir, ignore_errors=True)
            log.LOGGER(f"[CH][{vmachine_id}] event=cleanup runtime_dir_removed={runtime_dir}")
    except Exception as e:
        log.LOGGER(f"[CH][{vmachine_id}] failed removing runtime directory: {e}")

    delete_runtime_state(vmachine_id)
    log.LOGGER(f"[CH][{vmachine_id}] event=cleanup runtime_state_removed")
    return True
