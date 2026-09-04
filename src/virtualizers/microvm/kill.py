"""Tear down one microVM and everything the host is holding on its behalf.

One implementation for the whole family. It was two -- ``ch/kill.py`` and
``qemu/kill.py``, identical but for the log prefix, the socket's filename and
which module's process matcher was called -- and the copy is what made a QEMU
guest torn down by CH's teardown leave the emulator running with its socket
behind (#295).

Everything this touches is recorded in the VM's runtime state, so the sequence
does not depend on which hypervisor booted it: signal the process (only after
confirming the PID is still that process), drop the firewall rules by this VM's
comment prefix, delete the tap, remove the cgroup, unlink the control socket,
remove the runtime directory, release the virtiofs shares, and only then delete
the state file -- last, so an interrupted teardown is still visible to the
janitor rather than becoming an invisible directory holding a rootfs image.

Never raises: a teardown that gave up halfway on the first error it met would
leak everything after that point. Each step logs its own failure and the next one
runs.
"""
import os
import shutil
import signal
from pathlib import Path
from typing import List, Optional

from src.utils import logger as log
from src.virtualizers.microvm import network, paths
from src.virtualizers.microvm.cgroups import remove_vm_cgroup
from src.virtualizers.microvm.host import run
from src.virtualizers.microvm.hypervisor import Hypervisor
from src.virtualizers.microvm.process import pid_matches
from src.virtualizers.microvm.runtime_state import (
    delete_runtime_state,
    list_runtime_states,
    load_runtime_state,
    recorded_process_name,
)
from src.virtualizers.microvm.virtiofs import (
    shared_fs_base_dir,
    teardown_virtiofs_for_vm,
)


def _runtime_dir(vmachine_id: str) -> Optional[Path]:
    try:
        return paths.runtime_vm_dir(vmachine_id)
    except Exception:
        return None


def _kill_pid(hypervisor: Hypervisor, vmachine_id: str, pid: int, process_name: str) -> None:
    if pid <= 0:
        return
    if not pid_matches(pid=pid, process_name=process_name):
        log.LOGGER(hypervisor.log(
            vmachine_id,
            f"skip SIGKILL for pid={pid}: process is missing or no longer matches this VM",
        ))
        return
    try:
        os.kill(pid, signal.SIGKILL)
        log.LOGGER(hypervisor.log(vmachine_id, f"SIGKILL sent to pid={pid}"))
    except ProcessLookupError:
        log.LOGGER(hypervisor.log(vmachine_id, f"pid already dead: {pid}"))
    except PermissionError as e:
        log.LOGGER(hypervisor.log(vmachine_id, f"permission denied killing pid={pid}: {e}"))
    except Exception as e:
        log.LOGGER(hypervisor.log(vmachine_id, f"failed to kill pid={pid}: {e}"))


def _cleanup_vm_firewall_rules(hypervisor: Hypervisor, vmachine_id: str) -> None:
    """Delete every rule nodo wrote for this VM, by its comment prefix."""
    try:
        from src.virtualizers.firewall import remove_vm_rules

        removed = remove_vm_rules(vmachine_id=vmachine_id)
        if removed:
            log.LOGGER(hypervisor.log(vmachine_id, f"removed {removed} firewall rule(s)"))
    except Exception as e:
        log.LOGGER(hypervisor.log(vmachine_id, f"error removing firewall rules: {e}"))


def _cleanup_tap(hypervisor: Hypervisor, vmachine_id: str, tap_name: Optional[str]) -> None:
    if not tap_name:
        return
    try:
        run(["ip", "link", "del", str(tap_name)])
        log.LOGGER(hypervisor.log(vmachine_id, f"cleanup tap attempted: {tap_name}"))
    except Exception as e:
        log.LOGGER(hypervisor.log(vmachine_id, f"error cleaning tap {tap_name}: {e}"))


def kill(hypervisor: Hypervisor, vmachine_id: str) -> bool:
    state = load_runtime_state(vmachine_id) or {}
    pid = int(state.get("pid") or 0)
    tap_name = str(state.get("tap") or "")
    cleanup_rules: List[List[str]] = state.get("cleanup_rules") or []
    cgroup_path = str(state.get("cgroup_path") or "")
    runtime_dir = _runtime_dir(vmachine_id)
    control_socket = str(state.get("control_socket") or "").strip()
    process_name = recorded_process_name(state) or hypervisor.process_name(vmachine_id)

    log.LOGGER(hypervisor.log(vmachine_id, "event=kill requested"))
    _kill_pid(
        hypervisor=hypervisor, vmachine_id=vmachine_id, pid=pid, process_name=process_name
    )
    # Two paths on purpose. cleanup_rules is what pre-nftables versions recorded
    # and is empty for anything this version launched; the prefix sweep is what
    # removes the rules by the comment they carry, so a teardown cannot leave an
    # orphan DNAT behind because the recorded command no longer matches.
    network.replay_legacy_cleanup_rules(
        cleanup_rules, log_prefix=hypervisor.prefix(vmachine_id)
    )
    _cleanup_vm_firewall_rules(hypervisor=hypervisor, vmachine_id=vmachine_id)
    _cleanup_tap(hypervisor=hypervisor, vmachine_id=vmachine_id, tap_name=tap_name)
    remove_vm_cgroup(vmachine_id=vmachine_id, cgroup_path=cgroup_path)

    # The control socket lives in the short, flat socket directory rather than in
    # the runtime dir (an AF_UNIX path cannot hold the full id under CACHE), so
    # removing the runtime dir does not take it with it: unlink it explicitly. The
    # recorded path wins; the derived one covers a state file written before the
    # socket existed.
    socket_path = Path(control_socket) if control_socket else hypervisor.control_socket(vmachine_id)
    try:
        socket_path.unlink(missing_ok=True)
        log.LOGGER(hypervisor.log(vmachine_id, f"event=cleanup control_socket_removed={socket_path}"))
    except Exception as e:
        log.LOGGER(hypervisor.log(vmachine_id, f"failed removing control socket {socket_path}: {e}"))

    try:
        if runtime_dir and runtime_dir.exists():
            shutil.rmtree(runtime_dir, ignore_errors=True)
            log.LOGGER(hypervisor.log(vmachine_id, f"event=cleanup runtime_dir_removed={runtime_dir}"))
    except Exception as e:
        log.LOGGER(hypervisor.log(vmachine_id, f"failed removing runtime directory: {e}"))

    # Release shared-filesystem backends. A virtiofsd daemon is stopped only when
    # this was the last VM using its share on the host; the exported directory is
    # removed only for shares this VM itself exported (owned_share_ids), so a
    # departing child never deletes its parent's data.
    virtiofs_mounts = state.get("virtiofs") or []
    if virtiofs_mounts:
        try:
            other_states = {
                vid: s for vid, s in list_runtime_states().items() if vid != vmachine_id
            }
            teardown_virtiofs_for_vm(
                vmachine_id=vmachine_id,
                mounts_state=virtiofs_mounts,
                runtime_states=other_states,
                base_dir=str(shared_fs_base_dir(paths.cache_root())),
                owned_share_ids=state.get("exported_shares") or [],
                logger_fn=log.LOGGER,
            )
        except Exception as e:
            log.LOGGER(hypervisor.log(vmachine_id, f"virtiofs teardown error: {e}"))

    delete_runtime_state(vmachine_id)
    log.LOGGER(hypervisor.log(vmachine_id, "event=cleanup runtime_state_removed"))
    log.LOGGER(hypervisor.log(vmachine_id, "event=kill completed"))

    return True
