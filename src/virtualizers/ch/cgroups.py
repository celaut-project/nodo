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

    parent = _cgroup_root() / "nodo-ch"
    parent.mkdir(parents=True, exist_ok=True)

    available = _read_available_controllers(_cgroup_root())
    wanted = {"memory", "cpu"}.intersection(available)
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
