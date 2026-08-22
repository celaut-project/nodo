"""Tear down a QEMU guest.

The cleanup is identical to CH's -- same shared runtime-state store, same TAP,
DNAT, cgroup and virtiofs teardown helpers -- except the process is matched by
the QEMU visible name (``nodo-qemu-<id8>``) before it is signalled, so a recycled
PID is never killed by mistake.
"""
import os
import signal
import shutil
from pathlib import Path
from typing import List, Optional

from src.utils import logger as log
from src.utils.config import ConfigManager
from src.virtualizers.ch.cgroups import remove_vm_cgroup
from src.virtualizers.ch.kill import _cleanup_dnat_rules, _cleanup_tap
from src.virtualizers.ch.runtime_state import (
    delete_runtime_state,
    list_runtime_states,
    load_runtime_state,
)
from src.virtualizers.ch.virtiofs import (
    shared_fs_base_dir,
    teardown_virtiofs_for_vm,
)
from src.virtualizers.qemu.process import pid_matches_vmachine

env_manager = ConfigManager()
CACHE = env_manager.get("CACHE")


def _runtime_dir(vmachine_id: str) -> Optional[Path]:
    if not CACHE:
        return None
    return Path(CACHE) / "cloud_hypervisor" / "runtime" / vmachine_id


def _kill_pid(vmachine_id: str, pid: int) -> None:
    if pid <= 0:
        return
    if not pid_matches_vmachine(pid=pid, vmachine_id=vmachine_id):
        log.LOGGER(
            f"[QEMU][{vmachine_id}] skip SIGKILL for pid={pid}: "
            "process is missing or no longer matches this VM"
        )
        return
    try:
        os.kill(pid, signal.SIGKILL)
        log.LOGGER(f"[QEMU][{vmachine_id}] SIGKILL sent to pid={pid}")
    except ProcessLookupError:
        log.LOGGER(f"[QEMU][{vmachine_id}] pid already dead: {pid}")
    except PermissionError as e:
        log.LOGGER(f"[QEMU][{vmachine_id}] permission denied killing pid={pid}: {e}")
    except Exception as e:
        log.LOGGER(f"[QEMU][{vmachine_id}] failed to kill pid={pid}: {e}")



def _cleanup_vm_firewall_rules(vmachine_id: str) -> None:
    """Delete every rule nodo wrote for this VM, by its comment prefix."""
    try:
        from src.virtualizers.firewall import remove_vm_rules

        removed = remove_vm_rules(vmachine_id=vmachine_id)
        if removed:
            log.LOGGER(f"[QEMU][{vmachine_id}] removed {removed} firewall rule(s)")
    except Exception as e:
        log.LOGGER(f"[QEMU][{vmachine_id}] error removing firewall rules: {e}")

def kill(vmachine_id: str) -> bool:
    state = load_runtime_state(vmachine_id) or {}
    pid = int(state.get("pid") or 0)
    tap_name = str(state.get("tap") or "")
    cleanup_rules: List[List[str]] = state.get("cleanup_rules") or []
    cgroup_path = str(state.get("cgroup_path") or "")
    runtime_dir = _runtime_dir(vmachine_id)

    log.LOGGER(f"[QEMU][{vmachine_id}] event=kill requested")
    _kill_pid(vmachine_id=vmachine_id, pid=pid)
    # Two paths on purpose. cleanup_rules is what pre-nftables versions recorded
    # and is empty for anything this version launched; the prefix sweep is what
    # removes the rules by the comment they carry, so a teardown cannot leave an
    # orphan DNAT behind because the recorded command no longer matches.
    _cleanup_dnat_rules(vmachine_id=vmachine_id, cleanup_rules=cleanup_rules)
    _cleanup_vm_firewall_rules(vmachine_id=vmachine_id)
    _cleanup_tap(vmachine_id=vmachine_id, tap_name=tap_name)
    remove_vm_cgroup(vmachine_id=vmachine_id, cgroup_path=cgroup_path)

    try:
        if runtime_dir and runtime_dir.exists():
            shutil.rmtree(runtime_dir, ignore_errors=True)
            log.LOGGER(f"[QEMU][{vmachine_id}] event=cleanup runtime_dir_removed={runtime_dir}")
    except Exception as e:
        log.LOGGER(f"[QEMU][{vmachine_id}] failed removing runtime directory: {e}")

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
            log.LOGGER(f"[QEMU][{vmachine_id}] virtiofs teardown error: {e}")

    delete_runtime_state(vmachine_id)
    log.LOGGER(f"[QEMU][{vmachine_id}] event=kill completed")
    return True
