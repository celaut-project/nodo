import os
from pathlib import Path
from typing import Optional

from src.utils.config import ConfigManager

env_manager = ConfigManager()

CGROUPS_BASE_DIR = str(
    env_manager.get("virtualizers.cloud_hypervisor.CGROUPS_BASE_DIR", "/sys/fs/cgroup")
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


def ensure_vm_cgroup(vmachine_id: str, pid: int) -> Path:
    if not cgroup_v2_available():
        raise RuntimeError(
            f"cgroup v2 not available at {CGROUPS_BASE_DIR}; this phase supports only v2."
        )
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError(f"Invalid PID for vmachine {vmachine_id}: {pid}")

    vm_cgroup = _vm_cgroup_dir(vmachine_id)
    vm_cgroup.mkdir(parents=True, exist_ok=True)

    procs_file = vm_cgroup / "cgroup.procs"
    with open(procs_file, "w", encoding="utf-8") as f:
        f.write(f"{pid}\n")

    return vm_cgroup


def apply_memory_limit(vm_cgroup: Path, mem_limit: int) -> None:
    if mem_limit > 0:
        raw = str(int(mem_limit))
    else:
        raw = "max"
    with open(vm_cgroup / "memory.max", "w", encoding="utf-8") as f:
        f.write(f"{raw}\n")


def apply_cpu_limit(vm_cgroup: Path, cpu_quota: int, cpu_period: int) -> None:
    if cpu_period <= 0:
        raise RuntimeError(f"Invalid cpu_period for cpu.max: {cpu_period}")
    quota_raw = "max" if cpu_quota <= 0 else str(int(cpu_quota))
    with open(vm_cgroup / "cpu.max", "w", encoding="utf-8") as f:
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
