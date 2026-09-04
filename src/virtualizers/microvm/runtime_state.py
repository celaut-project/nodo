"""The family's runtime-state store: one enumerable index, private payloads.

One directory, one file per VM, keyed by ``vmachine_id``, shared by every
hypervisor in the family. Shared because what it tracks is shared: one bridge,
one subnet, one IP/MAC allocator that reads every entry to avoid handing out an
address twice, one janitor that has to find entries with no database row -- which
it cannot do by asking the database, so it must be able to enumerate them.

Two tiers, and the distinction is load-bearing:

**The index** -- ``vmachine_id``, ``virtualizer``, ``service_id``, ``pid``,
``process_name``, ``created_at``, ``booting``, ``ip`` -- is what a reader may
interpret without knowing which hypervisor wrote the entry. It is the minimum
that makes an entry judgeable: whose it is, whether its process is still the one
that was launched, whether it is old enough to judge at all.

**The payload** -- ``control_socket``, ``cgroup_path``, ``virtiofs``,
``dnat_rules``, ``guest_kernel_reserve_bytes``, ``boot_mem_bytes`` and the rest --
belongs to whoever wrote it. Readers outside the owning backend treat it as
opaque.

``process_name`` is in the index rather than derived by the reader on purpose.
Deriving it means knowing the launcher's naming convention, which is per
hypervisor, which is how CH's matcher came to be pointed at a QEMU guest and
reaped a healthy VM (#295). Recorded, every reader matches what was actually
launched and none of them dispatches to find out how.
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.virtualizers.microvm import paths
from src.virtualizers.microvm.hypervisor import Hypervisor

_LOCK_GUARD = threading.Lock()
_FILE_LOCKS: Dict[str, threading.Lock] = {}


def runtime_root() -> Path:
    """The directory holding every VM's state file and runtime directory.

    Public because more than the state file lives here: ``execute`` gives each VM
    a ``runtime/<vmachine_id>/`` directory (its own copy of the rootfs image, its
    serial log), and anything that reclaims disk has to walk those directories,
    not just the ``*.json`` beside them.
    """
    return paths.runtime_root()


def list_runtime_dirs() -> Dict[str, Path]:
    """Every ``runtime/<vmachine_id>/`` directory on disk, by vmachine id.

    Deliberately not derived from :func:`list_runtime_states`: the two can differ,
    and the difference is the leak. ``kill`` removes the directory and then the
    state file, so a teardown that dies between the two leaves a directory with no
    state — invisible to every reader that starts from the state files, and
    holding a full rootfs image.
    """
    runtime_dir = runtime_root()
    if not runtime_dir.exists():
        return {}

    dirs: Dict[str, Path] = {}
    for path in runtime_dir.iterdir():
        if path.is_dir() and not path.is_symlink():
            dirs[path.name] = path
    return dirs


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCK_GUARD:
        if key not in _FILE_LOCKS:
            _FILE_LOCKS[key] = threading.Lock()
        return _FILE_LOCKS[key]


def save_runtime_state(vmachine_id: str, payload: Dict[str, Any]) -> None:
    path = paths.runtime_state_file(vmachine_id)
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
    hypervisor: Hypervisor,
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
      rule while ``booting`` is set (see ``maintain.orphan_reason``).

    ``control_socket`` is deliberately absent: the hypervisor creates that socket a
    moment after it starts, and ``maintain`` reads a recorded-but-missing socket as
    a dead VM. The final write adds it, once it is there to be found.
    """
    save_runtime_state(
        vmachine_id,
        {
            "vmachine_id": vmachine_id,
            "virtualizer": hypervisor.name,
            "process_name": hypervisor.process_name(vmachine_id),
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
    path = paths.runtime_state_file(vmachine_id)
    if not path.is_file():
        return None

    lock = _lock_for(path)
    with lock:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def delete_runtime_state(vmachine_id: str) -> None:
    path = paths.runtime_state_file(vmachine_id)
    if not path.exists():
        return

    lock = _lock_for(path)
    with lock:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def list_runtime_states() -> Dict[str, Dict[str, Any]]:
    runtime_dir = runtime_root()
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


def recorded_process_name(state: Optional[Dict[str, Any]]) -> str:
    """The visible process name the launcher gave this VM, or ``""``.

    ``""`` means "do not check the name", which is what a caller holding an entry
    with no recorded name has to fall back to -- the alternative, guessing a
    prefix, is the bug this field exists to remove.
    """
    return str((state or {}).get("process_name") or "").strip()


def recorded_virtualizer(state: Optional[Dict[str, Any]]) -> str:
    """Which hypervisor wrote this entry, normalized, or ``""``."""
    return str((state or {}).get("virtualizer") or "").strip().lower()
