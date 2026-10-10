import os
from pathlib import Path
from typing import Optional

from src.utils.config import ConfigManager

env_manager = ConfigManager()

CGROUPS_BASE_DIR = str(
    env_manager.get("virtualizers.ch.CGROUPS_BASE_DIR", "/sys/fs/cgroup")
).strip()


def _cgroup_root() -> Path:
    return Path(CGROUPS_BASE_DIR)


def _vm_cgroup_dir(vmachine_id: str) -> Path:
    safe_id = str(vmachine_id).strip()
    if not safe_id:
        raise ValueError("vmachine_id is empty.")
    if "/" in safe_id:
        raise ValueError(f"Invalid vmachine_id for cgroup path: {vmachine_id}")
    return _cgroup_root() / "nodo-ch" / safe_id


def cgroup_v2_available() -> bool:
    return (_cgroup_root() / "cgroup.controllers").is_file()


CGROUP_MOUNT = Path("/sys/fs/cgroup")
# Where the daemon's own processes go when CGROUPS_BASE_DIR is its delegated
# cgroup. Not "nodo-ch": that name is the parent of the per-VM cgroups.
SUPERVISOR_LEAF = "supervisor"


def _own_cgroup(proc_cgroup: str = "/proc/self/cgroup") -> Optional[Path]:
    """This process's cgroup v2 directory, from the ``0::`` line of ``proc_cgroup``."""
    try:
        with open(proc_cgroup, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("0::"):
                    return CGROUP_MOUNT / line[3:].strip().lstrip("/")
    except OSError:
        return None
    return None


def leave_delegated_base() -> None:
    """Move this process out of ``CGROUPS_BASE_DIR`` when that is its own cgroup.

    A daemon that runs as a service user cannot write the root cgroup. It gets a
    cgroup of its own instead -- systemd's ``Delegate=yes`` makes the unit's
    cgroup owned by the unit's user -- and ``CGROUPS_BASE_DIR`` points at it. But
    cgroup v2 refuses to enable a controller for the children of a cgroup that
    holds processes, other than the root (the no-internal-process rule), and the
    daemon is in that cgroup. So its processes move to a ``supervisor`` leaf
    first; ``nodo-ch/<vm>`` is then created beside it. A no-op when the base is
    the root cgroup, which is the root install's default, or any cgroup this
    process is not in.
    """
    base = _cgroup_root()
    own = _own_cgroup()
    if own is None or own == CGROUP_MOUNT:
        return
    try:
        if os.path.realpath(own) != os.path.realpath(base):
            return
    except OSError:
        return
    leaf = base / SUPERVISOR_LEAF
    leaf.mkdir(exist_ok=True)
    try:
        with open(base / "cgroup.procs", "r", encoding="utf-8") as f:
            pids = [pid.strip() for pid in f.read().split() if pid.strip()]
    except OSError as e:
        raise RuntimeError(f"Unable to read the processes of {base}: {e}") from e
    for pid in pids:
        try:
            with open(leaf / "cgroup.procs", "w", encoding="utf-8") as f:
                f.write(f"{pid}\n")
        except ProcessLookupError:
            continue


def _read_available_controllers(path: Path) -> set[str]:
    controllers_file = path / "cgroup.controllers"
    if not controllers_file.is_file():
        return set()
    try:
        with open(controllers_file, "r", encoding="utf-8") as f:
            return {c.strip() for c in f.read().split() if c.strip()}
    except Exception:
        return set()


def _enable_subtree_controllers(parent: Path, wanted: set[str]) -> None:
    if not wanted:
        return
    subtree_control = parent / "cgroup.subtree_control"
    if not subtree_control.is_file():
        return
    try:
        with open(subtree_control, "r", encoding="utf-8") as f:
            enabled = {c.strip().lstrip("+") for c in f.read().split() if c.strip()}
    except Exception:
        enabled = set()

    missing = sorted(wanted.difference(enabled))
    if not missing:
        return

    payload = " ".join(f"+{controller}" for controller in missing)
    try:
        with open(subtree_control, "w", encoding="utf-8") as f:
            f.write(f"{payload}\n")
    except Exception as e:
        raise RuntimeError(
            f"Unable to enable cgroup subtree controllers {missing} in {parent}: {e}"
        ) from e


def ensure_vm_cgroup(vmachine_id: str, pid: int) -> Path:
    if not cgroup_v2_available():
        raise RuntimeError(
            f"cgroup v2 not available at {CGROUPS_BASE_DIR}; this phase supports only v2."
        )
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError(f"Invalid PID for vmachine {vmachine_id}: {pid}")

    leave_delegated_base()

    root = _cgroup_root()
    available = _read_available_controllers(root)
    wanted = {"memory", "cpu"}.intersection(available)

    # cgroup v2 delegation is top-down: a controller is only listed in a child's
    # cgroup.controllers (and thus enable-able in its cgroup.subtree_control) if
    # the PARENT already delegates it via cgroup.subtree_control. systemd usually
    # delegates cpu/memory at the root, but on nodes where it doesn't, enabling
    # them straight on nodo-ch fails with ENOENT ("Unable to enable cgroup subtree
    # controllers ['cpu', 'memory'] in .../nodo-ch"). Delegate on the root first so
    # nodo-ch inherits the controllers, then delegate on nodo-ch for the VM child.
    _enable_subtree_controllers(root, wanted)

    parent = root / "nodo-ch"
    parent.mkdir(parents=True, exist_ok=True)
    _enable_subtree_controllers(parent, wanted)

    vm_cgroup = _vm_cgroup_dir(vmachine_id)
    vm_cgroup.mkdir(parents=True, exist_ok=True)

    procs_file = vm_cgroup / "cgroup.procs"
    with open(procs_file, "w", encoding="utf-8") as f:
        f.write(f"{pid}\n")  # Writing to cgroup.procs instructs the kernel to move this process into the cgroup. 

    return vm_cgroup


def apply_memory_limit(vm_cgroup: Path, mem_limit: int) -> None:
    memory_max_file = vm_cgroup / "memory.max"
    if not memory_max_file.is_file():
        raise RuntimeError(
            f"memory controller not delegated/available for cgroup: {vm_cgroup}"
        )
    if mem_limit > 0:
        raw = str(int(mem_limit))
    else:
        raw = "max"
    with open(memory_max_file, "w", encoding="utf-8") as f:
        f.write(f"{raw}\n")


def apply_cpu_limit(vm_cgroup: Path, cpu_quota: int, cpu_period: int) -> None:
    cpu_max_file = vm_cgroup / "cpu.max"
    if not cpu_max_file.is_file():
        raise RuntimeError(
            f"cpu controller not delegated/available for cgroup: {vm_cgroup}"
        )
    if cpu_period <= 0:
        raise RuntimeError(f"Invalid cpu_period for cpu.max: {cpu_period}")
    quota_raw = "max" if cpu_quota <= 0 else str(int(cpu_quota))
    with open(cpu_max_file, "w", encoding="utf-8") as f:
        f.write(f"{quota_raw} {int(cpu_period)}\n")


def remove_vm_cgroup(vmachine_id: str, cgroup_path: Optional[str] = None) -> None:
    if cgroup_path and str(cgroup_path).strip():
        vm_cgroup = Path(str(cgroup_path))
    else:
        vm_cgroup = _vm_cgroup_dir(vmachine_id)

    if not vm_cgroup.exists():
        return

    try:
        # Best effort: ensure no process stays pinned.
        procs_file = vm_cgroup / "cgroup.procs"
        if procs_file.exists():
            with open(procs_file, "r", encoding="utf-8") as f:
                if f.read().strip():
                    return
    except Exception:
        # Ignore read failures; still attempt cleanup.
        pass

    try:
        os.rmdir(vm_cgroup)
    except OSError:
        pass
