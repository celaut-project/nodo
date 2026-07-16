import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from src.utils import logger as log
from src.utils.config import ConfigManager
from src.virtualizers.ch.cgroups import remove_vm_cgroup
from src.virtualizers.ch.process import pid_matches_vmachine
from src.virtualizers.ch.runtime_state import (
    delete_runtime_state,
    list_runtime_states,
    load_runtime_state,
)
from src.virtualizers.ch.virtiofs import (
    shared_fs_base_dir,
    teardown_virtiofs_for_vm,
)

env_manager = ConfigManager()
CACHE = env_manager.get("CACHE")
CH_API_SOCKET_DIR = env_manager.get("virtualizers.ch.API_SOCKET_DIR", "/tmp/nodo-ch")

def _runtime_vm_dir(vmachine_id: str) -> Path:
    return Path(CACHE) / "cloud_hypervisor" / "runtime" / vmachine_id


def _service_bundle_dir(service_id: str) -> Path:
    return Path(CACHE) / "cloud_hypervisor" / service_id


def _remove(vmachine_id: str) -> bool:
    if not CACHE:
        log.LOGGER(f"[CH][{vmachine_id}] remove failed: CACHE path is not configured.")
        return False

    # Dual mode: runtime VM cleanup if state/runtime exists; otherwise remove service bundle.
    if load_runtime_state(vmachine_id) is not None or _runtime_vm_dir(vmachine_id).exists():
        log.LOGGER(f"[CH][{vmachine_id}] event=remove mode=runtime")
        return kill(vmachine_id=vmachine_id)

    bundle_dir = _service_bundle_dir(vmachine_id)
    if not bundle_dir.exists():
        log.LOGGER(
            f"[CH][{vmachine_id}] remove requested but no runtime state nor service bundle directory found."
        )
        return True

    try:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        log.LOGGER(f"[CH][{vmachine_id}] event=remove mode=bundle bundle_removed={bundle_dir}")
        return True
    except Exception as e:
        log.LOGGER(f"[CH][{vmachine_id}] remove bundle failed ({bundle_dir}): {e}")
        return False


def _runtime_dir(vmachine_id: str) -> Optional[Path]:
    if not CACHE:
        return None
    return Path(CACHE) / "cloud_hypervisor" / "runtime" / vmachine_id


def _api_socket_path(vmachine_id: str) -> Path:
    return Path(CH_API_SOCKET_DIR) / f"ch-{vmachine_id[:16]}.sock"

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
    try:
        _run(["ip", "link", "del", str(tap_name)])
        log.LOGGER(f"[CH][{vmachine_id}] cleanup tap attempted: {tap_name}")
    except Exception as e:
        log.LOGGER(f"[CH][{vmachine_id}] error cleaning tap {tap_name}: {e}")


def _kill_pid(vmachine_id: str, pid: int) -> None:
    if pid <= 0:
        return
    if not pid_matches_vmachine(pid=pid, vmachine_id=vmachine_id):
        log.LOGGER(
            f"[CH][{vmachine_id}] skip SIGKILL for pid={pid}: "
            "process is missing or no longer matches this VM"
        )
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
    api_socket = str(state.get("api_socket") or "").strip()

    log.LOGGER(f"[CH][{vmachine_id}] event=kill requested")
    _kill_pid(vmachine_id=vmachine_id, pid=pid)
    _cleanup_dnat_rules(vmachine_id=vmachine_id, cleanup_rules=cleanup_rules)
    _cleanup_tap(vmachine_id=vmachine_id, tap_name=tap_name)
    remove_vm_cgroup(vmachine_id=vmachine_id, cgroup_path=cgroup_path)

    socket_path = Path(api_socket) if api_socket else _api_socket_path(vmachine_id)
    try:
        socket_path.unlink(missing_ok=True)
        log.LOGGER(f"[CH][{vmachine_id}] event=cleanup api_socket_removed={socket_path}")
    except Exception as e:
        log.LOGGER(f"[CH][{vmachine_id}] failed removing API socket {socket_path}: {e}")

    try:
        if runtime_dir and runtime_dir.exists():
            shutil.rmtree(runtime_dir, ignore_errors=True)
            log.LOGGER(f"[CH][{vmachine_id}] event=cleanup runtime_dir_removed={runtime_dir}")
    except Exception as e:
        log.LOGGER(f"[CH][{vmachine_id}] failed removing runtime directory: {e}")

    # Release shared-filesystem backends. A virtiofsd daemon is stopped only when
    # this was the last VM using its share on the host; the exported directory is
    # removed only for shares this VM itself exported (owned_share_ids), so a
    # departing child never deletes its parent's data.
    virtiofs_mounts = state.get("virtiofs") or []
    if virtiofs_mounts and CACHE:
        try:
            other_states = {
                vid: s for vid, s in list_runtime_states().items() if vid != vmachine_id
            }
            teardown_virtiofs_for_vm(
                vmachine_id=vmachine_id,
                mounts_state=virtiofs_mounts,
                runtime_states=other_states,
                base_dir=str(shared_fs_base_dir(CACHE)),
                owned_share_ids=state.get("exported_shares") or [],
                logger_fn=log.LOGGER,
            )
        except Exception as e:
            log.LOGGER(f"[CH][{vmachine_id}] virtiofs teardown error: {e}")

    delete_runtime_state(vmachine_id)
    log.LOGGER(f"[CH][{vmachine_id}] event=cleanup runtime_state_removed")

    _remove(vmachine_id=vmachine_id)
    log.LOGGER(f"[CH][{vmachine_id}] event=kill completed")

    return True
