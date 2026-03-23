import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from src.utils import logger as log
from src.utils.config import ConfigManager
from src.virtualizers.cloud_hypervisor.cgroups import remove_vm_cgroup
from src.virtualizers.cloud_hypervisor.runtime_state import (
    delete_runtime_state,
    load_runtime_state,
)

env_manager = ConfigManager()
CACHE = env_manager.get("CACHE")


def _runtime_dir(vmachine_id: str) -> Optional[Path]:
    if not CACHE:
        return None
    return Path(CACHE) / "cloud_hypervisor" / "runtime" / vmachine_id


def _run(command: List[str]) -> None:
    try:
        subprocess.run(command, check=False, capture_output=True, text=True)
    except Exception:
        pass


def _cleanup_dnat_rules(vmachine_id: str, dnat_rules: Iterable[Dict[str, object]]) -> None:
    for rule in dnat_rules or []:
        try:
            protocol = str(rule.get("protocol", "")).strip().lower()
            external_port = int(rule.get("external_port"))
            internal_port = int(rule.get("internal_port"))
            destination_ip = str(rule.get("destination_ip", "")).strip()
            if protocol not in {"tcp", "udp"}:
                continue
            if not destination_ip:
                continue

            _run(
                [
                    "iptables",
                    "-t",
                    "nat",
                    "-D",
                    "PREROUTING",
                    "-p",
                    protocol,
                    "--dport",
                    str(external_port),
                    "-j",
                    "DNAT",
                    "--to-destination",
                    f"{destination_ip}:{internal_port}",
                ]
            )
            _run(
                [
                    "iptables",
                    "-t",
                    "nat",
                    "-D",
                    "OUTPUT",
                    "-p",
                    protocol,
                    "--dport",
                    str(external_port),
                    "-j",
                    "DNAT",
                    "--to-destination",
                    f"{destination_ip}:{internal_port}",
                ]
            )
            _run(
                [
                    "iptables",
                    "-D",
                    "FORWARD",
                    "-p",
                    protocol,
                    "-d",
                    destination_ip,
                    "--dport",
                    str(internal_port),
                    "-j",
                    "ACCEPT",
                ]
            )
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
    dnat_rules = state.get("dnat_rules") or []
    cgroup_path = str(state.get("cgroup_path") or "")
    runtime_dir = _runtime_dir(vmachine_id)

    log.LOGGER(f"[CH][{vmachine_id}] kill requested")
    _kill_pid(vmachine_id=vmachine_id, pid=pid)
    _cleanup_dnat_rules(vmachine_id=vmachine_id, dnat_rules=dnat_rules)
    _cleanup_tap(vmachine_id=vmachine_id, tap_name=tap_name)
    remove_vm_cgroup(vmachine_id=vmachine_id, cgroup_path=cgroup_path)

    try:
        if runtime_dir and runtime_dir.exists():
            shutil.rmtree(runtime_dir, ignore_errors=True)
            log.LOGGER(f"[CH][{vmachine_id}] runtime directory removed: {runtime_dir}")
    except Exception as e:
        log.LOGGER(f"[CH][{vmachine_id}] failed removing runtime directory: {e}")

    delete_runtime_state(vmachine_id)
    log.LOGGER(f"[CH][{vmachine_id}] runtime state removed")
    return True
