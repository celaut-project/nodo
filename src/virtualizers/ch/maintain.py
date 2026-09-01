import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.database.sql_connection import SQLConnection
from src.utils import logger as log
from src.virtualizers.ch.kill import kill as kill_ch_vm
from src.virtualizers.ch.process import pid_alive
from src.virtualizers.ch.runtime_state import load_runtime_state
from src.virtualizers.ch.runtime_state import list_runtime_dirs
from src.virtualizers.ch.runtime_state import list_runtime_states
from src.virtualizers.selection import QEMU

sc = SQLConnection()

# A runtime directory exists before the state file that describes it: `execute`
# creates `runtime/<id>/` to unpack the rootfs into, and only writes the booting
# state minutes later, once the hypervisor process exists. A directory with no
# state is therefore ambiguous -- a launch still preparing its image, or the
# debris of one that died. Age settles it: nothing takes this long to reach
# `save_booting_state`, and a prune that guessed wrong would delete the image a
# live launch is still writing.
STATELESS_RUNTIME_GRACE_SECONDS = 6 * 60 * 60


def _liveness_for(state) -> Callable[..., bool]:
    """The ``pid_alive`` belonging to the backend that launched this VM.

    Both backends share this runtime-state directory, and every entry records
    which one wrote it. Liveness, however, is not backend-agnostic: it confirms
    the PID still belongs to *this* VM by matching the launcher's visible process
    name, and those names differ (``nodo-ch-<id8>`` vs ``nodo-qemu-<id8>``). So a
    QEMU guest checked with CH's matcher fails the name test while perfectly
    alive, and the janitor reaps a healthy VM as ``stale_runtime_process_dead``.

    Dispatch on the recorded ``virtualizer`` instead. The import is local because
    the QEMU backend imports this module's package at import time.
    """
    if str((state or {}).get("virtualizer") or "").strip().lower() == QEMU:
        from src.virtualizers.qemu.process import pid_alive as qemu_pid_alive

        return qemu_pid_alive
    return pid_alive


def _kill_for(state) -> Callable[..., bool]:
    """The teardown belonging to the backend that launched this VM.

    Same reason as :func:`_liveness_for`: CH's kill looks for CH's process and
    CH's api socket, so pointing it at a QEMU guest leaves the emulator running
    and its QMP socket behind while the state file disappears.
    """
    if str((state or {}).get("virtualizer") or "").strip().lower() == QEMU:
        from src.virtualizers.qemu.kill import kill as qemu_kill

        return qemu_kill
    return kill_ch_vm


def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    state = load_runtime_state(vmachine_id)
    if not state:
        log.LOGGER(f"[CH][{vmachine_id}] event=maintain unhealthy reason=runtime_state_missing")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    pid = int(state.get("pid") or 0)
    if pid <= 0:
        log.LOGGER(f"[CH][{vmachine_id}] event=maintain unhealthy reason=invalid_pid pid={pid}")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    if not pid_alive(pid, vmachine_id=vmachine_id):
        log.LOGGER(f"[CH][{vmachine_id}] event=maintain unhealthy reason=process_dead pid={pid}")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    api_socket = str(state.get("api_socket") or "").strip()
    if api_socket and not os.path.exists(api_socket):
        log.LOGGER(
            f"[CH][{vmachine_id}] event=maintain unhealthy "
            f"reason=api_socket_missing pid={pid} socket={api_socket}"
        )
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    if debug_mode:
        log.LOGGER(
            f"[CH][{vmachine_id}] event=maintain healthy pid={pid}, api_socket={api_socket or '<none>'}"
        )


def orphan_reason(vmachine_id: str, state) -> Optional[str]:
    """Why this runtime state is dead weight, or ``None`` if the VM is healthy.

    The one definition of "orphan" in the codebase. The janitor acts on it on its
    own schedule; ``nodo prune`` reports and acts on it on demand. Two callers
    that each decided for themselves what an orphan is would eventually disagree,
    and the disagreement would be an operator watching `prune` list a VM it then
    refuses to remove.
    """
    pid = int((state or {}).get("pid") or 0)
    in_db = sc.internal_instance_exists(id=vmachine_id)
    is_alive = _liveness_for(state)
    alive = is_alive(pid, vmachine_id=vmachine_id) if pid > 0 else False
    # `execute` writes the state file the instant the hypervisor process exists
    # and registers the instance immediately after (see `save_booting_state`),
    # so for the width of those two writes a live VM legitimately has no row
    # yet. Killing it there would race the launcher for the VM it is still
    # setting up. Nothing leaks: a boot that fails deletes its own state, and a
    # process that dies is still caught below, `booting` or not.
    booting = bool((state or {}).get("booting")) and alive

    if not in_db and not booting:
        return "orphan_runtime_state"
    if not alive:
        return "stale_runtime_process_dead"
    return None


def janitor_cleanup_orphans(debug_mode: bool = False) -> None:
    states = list_runtime_states()
    if not states:
        return

    for vmachine_id, state in states.items():
        reason = orphan_reason(vmachine_id=vmachine_id, state=state)
        if not reason:
            continue

        pid = int((state or {}).get("pid") or 0)
        in_db = sc.internal_instance_exists(id=vmachine_id)
        alive = _liveness_for(state)(pid, vmachine_id=vmachine_id) if pid > 0 else False

        log.LOGGER(
            f"[CH][{vmachine_id}] event=janitor cleanup_start "
            f"reason={reason} in_db={in_db} pid={pid} alive={alive}"
        )
        try:
            _kill_for(state)(vmachine_id=vmachine_id)
            log.LOGGER(
                f"[CH][{vmachine_id}] event=janitor cleanup_done "
                f"reason={reason}"
            )
        except Exception as e:
            log.LOGGER(
                f"[CH][{vmachine_id}] event=janitor cleanup_failed "
                f"reason={reason} error={e}"
            )
            if debug_mode:
                raise


# --- On-demand disk reclamation (`nodo prune`) -------------------------------
#
# The janitor above kills orphaned VMs on its own schedule, which reclaims the
# runtime directory as a side effect of `kill`. Two things it never touches:
#
#   * a runtime directory whose state file is already gone -- `kill` removes the
#     directory first and the state second, so a teardown interrupted between the
#     two leaves a full rootfs image that no state-file reader will ever see
#     again;
#   * `failures/`, which is written on purpose (CONSERVE_RUNTIME_DIR_ON_FAILURE)
#     and pruned by nobody.
#
# What follows reports both, and removes them when asked. Every entry carries its
# size and its reason, so `nodo prune --dry-run` is a full account of what a run
# would delete and why.


@dataclass
class PruneEntry:
    """One reclaimable thing on disk: what it is, why, and what it costs."""

    kind: str  # "runtime" | "failure"
    vmachine_id: str
    path: Path
    reason: str
    size_bytes: int
    age_seconds: Optional[float] = None
    removed: bool = False
    error: Optional[str] = None


def _dir_size(path: Path) -> int:
    from src.virtualizers.ch.build import _dir_size_bytes

    return _dir_size_bytes(path)


def _age_seconds(path: Path, now: Optional[float] = None) -> Optional[float]:
    try:
        return max(0.0, (now if now is not None else time.time()) - path.stat().st_mtime)
    except OSError:
        return None


def _failures_root() -> Optional[Path]:
    from src.virtualizers.ch.build import CACHE

    if not CACHE:
        return None
    return Path(CACHE) / "cloud_hypervisor" / "failures"


def scan_orphan_runtimes() -> List[PruneEntry]:
    """Runtime entries this node can reclaim, newest condition first.

    Two shapes, both covered:

    * a state file whose VM is an orphan by :func:`orphan_reason` -- the janitor's
      own condition, reported here rather than waited for;
    * a directory with no state file at all, old enough that no launch could still
      be preparing it (:data:`STATELESS_RUNTIME_GRACE_SECONDS`). This is the one
      the janitor structurally cannot see, and on a node that has had teardowns
      interrupted it is the one holding the disk.
    """
    entries: List[PruneEntry] = []
    now = time.time()

    states = list_runtime_states()
    dirs = list_runtime_dirs()

    for vmachine_id, state in states.items():
        reason = orphan_reason(vmachine_id=vmachine_id, state=state)
        if not reason:
            continue
        path = dirs.get(vmachine_id)
        entries.append(
            PruneEntry(
                kind="runtime",
                vmachine_id=vmachine_id,
                path=path if path is not None else Path(""),
                reason=reason,
                size_bytes=_dir_size(path) if path is not None else 0,
                age_seconds=_age_seconds(path, now) if path is not None else None,
            )
        )

    for vmachine_id, path in dirs.items():
        if vmachine_id in states:
            continue
        age = _age_seconds(path, now)
        if age is not None and age < STATELESS_RUNTIME_GRACE_SECONDS:
            continue
        entries.append(
            PruneEntry(
                kind="runtime",
                vmachine_id=vmachine_id,
                path=path,
                reason="runtime_dir_without_state",
                size_bytes=_dir_size(path),
                age_seconds=age,
            )
        )

    entries.sort(key=lambda e: e.size_bytes, reverse=True)
    return entries


def scan_failures(retention_seconds: Optional[float], now: Optional[float] = None) -> Tuple[List[PruneEntry], List[PruneEntry]]:
    """``failures/`` entries, split into (reclaimable, kept).

    ``retention_seconds`` of ``None`` means keep nothing -- ``--all``. Anything
    younger than the window is returned in the second list with the reason it was
    kept, because a prune that silently skipped half of `failures/` and printed a
    small number would read as "there was nothing there".
    """
    root = _failures_root()
    if root is None or not root.is_dir():
        return [], []

    now = now if now is not None else time.time()
    prunable: List[PruneEntry] = []
    kept: List[PruneEntry] = []

    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        age = _age_seconds(path, now)
        entry = PruneEntry(
            kind="failure",
            vmachine_id=path.name,
            path=path,
            reason="failure_debris",
            size_bytes=_dir_size(path),
            age_seconds=age,
        )
        if retention_seconds is None:
            prunable.append(entry)
        elif age is not None and age >= retention_seconds:
            prunable.append(entry)
        else:
            days = retention_seconds / 86400.0
            entry.reason = f"within_retention_window ({days:.0f}d)"
            kept.append(entry)

    prunable.sort(key=lambda e: e.size_bytes, reverse=True)
    kept.sort(key=lambda e: e.size_bytes, reverse=True)
    return prunable, kept


def _remove_tree(path: Path) -> Tuple[int, Optional[str]]:
    """Delete ``path``; return (bytes actually freed, error message or None).

    Freed is measured, not assumed: a tree that fails to delete halfway must not
    be reported as fully reclaimed.
    """
    before = _dir_size(path)
    try:
        shutil.rmtree(path)
    except Exception as e:
        after = _dir_size(path) if path.exists() else 0
        return max(0, before - after), str(e)
    if path.exists():
        return max(0, before - _dir_size(path)), "removal left files behind"
    return before, None


def reclaim(entry: PruneEntry) -> PruneEntry:
    """Remove what ``entry`` describes, recording what was actually freed.

    An orphan with a state file goes through ``kill``, not ``rmtree``: it is a VM,
    and its teardown also drops the firewall rules, the tap device, the cgroup and
    the API socket it left behind. Deleting only its directory would reclaim the
    disk and leak everything else.
    """
    if entry.kind == "runtime" and entry.reason != "runtime_dir_without_state":
        state = load_runtime_state(entry.vmachine_id) or {}
        try:
            _kill_for(state)(vmachine_id=entry.vmachine_id)
            entry.removed = True
        except Exception as e:
            entry.error = str(e)
            entry.removed = False
        if entry.removed and entry.path and entry.path.exists():
            freed, error = _remove_tree(entry.path)
            entry.size_bytes = freed
            entry.error = error
        log.LOGGER(
            f"[CH][{entry.vmachine_id}] event=prune kind=runtime reason={entry.reason} "
            f"removed={entry.removed} freed_bytes={entry.size_bytes} error={entry.error or 'none'}"
        )
        return entry

    freed, error = _remove_tree(entry.path)
    entry.size_bytes = freed
    entry.error = error
    entry.removed = error is None
    log.LOGGER(
        f"[CH][{entry.vmachine_id}] event=prune kind={entry.kind} reason={entry.reason} "
        f"removed={entry.removed} freed_bytes={freed} error={error or 'none'}"
    )
    return entry
