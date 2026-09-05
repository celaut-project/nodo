"""Judging a microVM: is it healthy, is it dead weight, and what can be reclaimed.

Three readers of the same store, and one definition of each question so they
cannot disagree:

* :func:`maintain` -- the maintenance tick's per-instance health check, asked
  about instances the database knows about.
* :func:`sweep_orphans` -- the janitor, which has the opposite problem: it looks
  for entries the database *does not* know about, so it cannot ask the database
  who owns what and has to enumerate the store instead.
* :func:`scan_orphan_runtimes` / :func:`scan_failures` -- ``nodo prune``, which
  reports the same conditions on demand and adds the two the janitor structurally
  cannot see.

One implementation for the whole family, not one per hypervisor. It used to be
one per hypervisor because liveness needs the launcher's visible process name and
those differ -- so the janitor, having only a state file, guessed, guessed CH, and
reaped a healthy QEMU guest (#295). The name is now recorded in the state
(``runtime_state``'s index), which makes the judgement uniform: what differs
between backends is not *how* a guest is judged but *what it recorded*, and once
it is recorded there is nothing left to dispatch on.
"""
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.database.sql_connection import SQLConnection
from src.utils import logger as log
from src.virtualizers.microvm import paths
from src.virtualizers.microvm.guest_panic import guest_panic_line
from src.virtualizers.microvm.hypervisor import Hypervisor
from src.virtualizers.microvm.kill import kill as kill_vm
from src.virtualizers.microvm.members import member
from src.virtualizers.microvm.process import pid_alive
from src.virtualizers.microvm.runtime_state import (
    list_runtime_dirs,
    list_runtime_states,
    load_runtime_state,
    recorded_process_name,
    recorded_virtualizer,
)

sc = SQLConnection()

# Used only where a log line is about the store rather than about one member's
# guest -- an entry no member claims.
FAMILY_LOG_TAG = "microvm"

# A runtime directory exists before the state file that describes it: `execute`
# creates `runtime/<id>/` to unpack the rootfs into, and only writes the booting
# state minutes later, once the hypervisor process exists. A directory with no
# state is therefore ambiguous -- a launch still preparing its image, or the
# debris of one that died. Age settles it: nothing takes this long to reach
# `save_booting_state`, and a prune that guessed wrong would delete the image a
# live launch is still writing.
STATELESS_RUNTIME_GRACE_SECONDS = 6 * 60 * 60


def _state_is_alive(state: Optional[Dict[str, Any]]) -> bool:
    """Is the process this entry recorded still that process?

    Matched against the recorded name, so a recycled PID is not mistaken for a
    live guest and a guest of one backend is not judged by another's naming.
    """
    pid = int((state or {}).get("pid") or 0)
    if pid <= 0:
        return False
    return pid_alive(pid, process_name=recorded_process_name(state))


def maintain(
    hypervisor: Hypervisor,
    vmachine_id: str,
    debug_mode: bool,
    remove_and_penalize: Callable[[str], None],
) -> None:
    """Health-check one running instance, removing and penalizing a dead one.

    Four ways a guest is gone, in the order they can be detected most cheaply:
    no runtime state, no usable PID, a process that is not this VM's any more,
    and a control socket the hypervisor no longer holds. A panicked kernel passes
    every one of them -- the process is alive and the socket answers while the
    guest inside is gone, holding its memory and, under emulation, a host core --
    so it is checked last and killed here rather than left to teardown.
    """
    state = load_runtime_state(vmachine_id)
    if not state:
        log.LOGGER(hypervisor.log(vmachine_id, "event=maintain unhealthy reason=runtime_state_missing"))
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    pid = int(state.get("pid") or 0)
    if pid <= 0:
        log.LOGGER(hypervisor.log(vmachine_id, f"event=maintain unhealthy reason=invalid_pid pid={pid}"))
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    if not _state_is_alive(state):
        log.LOGGER(hypervisor.log(vmachine_id, f"event=maintain unhealthy reason=process_dead pid={pid}"))
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    control_socket = str(state.get("control_socket") or "").strip()
    if control_socket and not Path(control_socket).exists():
        log.LOGGER(hypervisor.log(
            vmachine_id,
            f"event=maintain unhealthy reason=control_socket_missing pid={pid} socket={control_socket}",
        ))
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    panic = guest_panic_line(state)
    if panic:
        log.LOGGER(hypervisor.log(
            vmachine_id,
            f"event=maintain unhealthy reason=guest_panicked pid={pid} panic={panic!r}",
        ))
        # Killed here rather than left to the teardown below: `stop_instance`
        # only kills what it finds registered, and this branch exists precisely
        # for a process no liveness test will flag again. A double kill is
        # harmless; a missed one pins the resources for good.
        try:
            kill_vm(hypervisor, vmachine_id=vmachine_id)
        except Exception as e:
            log.LOGGER(hypervisor.log(vmachine_id, f"event=maintain panic_kill_failed error={e}"))
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    if debug_mode:
        log.LOGGER(hypervisor.log(
            vmachine_id,
            f"event=maintain healthy pid={pid}, control_socket={control_socket or '<none>'}",
        ))


def orphan_reason(vmachine_id: str, state) -> Optional[str]:
    """Why this runtime state is dead weight, or ``None`` if the VM is healthy.

    The one definition of "orphan" in the codebase. The janitor acts on it on its
    own schedule; ``nodo prune`` reports and acts on it on demand. Two callers
    that each decided for themselves what an orphan is would eventually disagree,
    and the disagreement would be an operator watching `prune` list a VM it then
    refuses to remove.
    """
    in_db = sc.internal_instance_exists(id=vmachine_id)
    alive = _state_is_alive(state)
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
    # A guest that panicked is dead weight whose process is still running, so it
    # is invisible to every check above. Excluded while `booting` for the same
    # reason as the row check: the launcher is still driving that VM, and it has
    # its own readiness deadline for giving up on it.
    if not booting and guest_panic_line(state):
        return "guest_panicked"
    return None


def sweep_orphans(debug_mode: bool = False) -> None:
    """Kill every VM in this family's store that no longer has an owner.

    The family's answer to "sweep your own orphans": it enumerates one shared
    store because its members share one, and dispatches each entry's teardown to
    the hypervisor that entry names. A family with no local store -- a remote
    backend whose runtime state is a handle to someone else's API -- implements
    the same question by asking that API instead, and never reads a directory.
    """
    states = list_runtime_states()
    if not states:
        return

    for vmachine_id, state in states.items():
        reason = orphan_reason(vmachine_id=vmachine_id, state=state)
        if not reason:
            continue

        recorded = recorded_virtualizer(state)
        hypervisor = member(recorded)
        if hypervisor is None:
            # Not this family's to tear down, and guessing is what #295 did.
            log.LOGGER(
                f"[{FAMILY_LOG_TAG}][{vmachine_id}] event=janitor skipped "
                f"reason={reason} unknown_virtualizer={recorded or '<unset>'}"
            )
            continue

        pid = int((state or {}).get("pid") or 0)
        in_db = sc.internal_instance_exists(id=vmachine_id)
        alive = _state_is_alive(state)

        log.LOGGER(hypervisor.log(
            vmachine_id,
            f"event=janitor cleanup_start reason={reason} in_db={in_db} pid={pid} alive={alive}",
        ))
        try:
            kill_vm(hypervisor, vmachine_id=vmachine_id)
            log.LOGGER(hypervisor.log(vmachine_id, f"event=janitor cleanup_done reason={reason}"))
        except Exception as e:
            log.LOGGER(hypervisor.log(vmachine_id, f"event=janitor cleanup_failed reason={reason} error={e}"))
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
    from src.virtualizers.microvm.build import _dir_size_bytes

    return _dir_size_bytes(path)


def _age_seconds(path: Path, now: Optional[float] = None) -> Optional[float]:
    try:
        return max(0.0, (now if now is not None else time.time()) - path.stat().st_mtime)
    except OSError:
        return None


def _failures_root() -> Optional[Path]:
    return paths.failures_root() if paths.optional_family_root() else None


def scan_orphan_runtimes() -> List[PruneEntry]:
    """Runtime entries this node can reclaim, largest first.

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

    A runtime entry is a VM, not a directory, so it is never reclaimed with
    ``rmtree`` alone -- its teardown also drops the firewall rules, the tap
    device, the cgroup and the control socket it left behind, and deleting only
    its directory would reclaim the disk and leak all of that.

    Which teardown depends on one question, and it is not what killed the VM:
    does the database still have a row for it?

    * It does -- ``stop_instance``, which kills the VM *and* purges the row and
      hands the child's unspent deposit back to its father. The reason this is
      not a detail: an entry can reach here with a live process and a live row
      (``guest_panicked`` -- a kernel that died inside a guest whose process and
      registration are both perfectly alive), and reclaiming that with ``kill``
      leaves the row and its MU with nothing running behind them, reconciled
      only if and when the maintenance tick next runs. On a node whose ``serve``
      is stopped, that is never (#326).
    * It does not -- ``kill``, the host-side teardown on its own. There is no
      row to purge and no deposit to return.
    """
    if entry.kind == "runtime" and entry.reason != "runtime_dir_without_state":
        state = load_runtime_state(entry.vmachine_id) or {}
        hypervisor = member(recorded_virtualizer(state))
        if hypervisor is None:
            entry.removed = False
            entry.error = (
                f"unknown virtualizer {recorded_virtualizer(state) or '<unset>'}; "
                "not this family's to tear down"
            )
            log.LOGGER(
                f"[{FAMILY_LOG_TAG}][{entry.vmachine_id}] event=prune kind=runtime "
                f"reason={entry.reason} removed=False error={entry.error}"
            )
            return entry

        try:
            registered = sc.internal_instance_exists(id=entry.vmachine_id)
        except Exception as e:
            # Without the answer there is no safe move: kill a VM that turns out
            # to have had a row and its deposit stays on the books as MU the
            # father paid for something no longer running, which is the
            # direction the accounting must never err in. Left alone and
            # reported, it is still here for the next run.
            entry.removed = False
            entry.error = f"cannot tell whether it is registered: {e}"
            log.LOGGER(hypervisor.log(
                entry.vmachine_id,
                f"event=prune kind=runtime reason={entry.reason} removed=False "
                f"error={entry.error}",
            ))
            return entry

        try:
            if registered:
                # Imported here rather than at module scope: the manager imports
                # the virtualizer interface, which imports this family.
                from src.manager.manager import stop_instance

                stop_instance(token=entry.vmachine_id)
                # Checked rather than assumed: `stop_instance` swallows a failed
                # purge and returns None, and reporting a stop that left the row
                # alive as done is how a balance nobody is spending goes
                # unnoticed. The maintenance tick retries what is still here.
                if sc.internal_instance_exists(id=entry.vmachine_id):
                    raise RuntimeError("stop_instance left the database row in place")
            else:
                kill_vm(hypervisor, vmachine_id=entry.vmachine_id)
            entry.removed = True
        except Exception as e:
            entry.error = str(e)
            entry.removed = False
        # Both teardowns remove the runtime directory themselves; this is what
        # happens when one of them got as far as the database and not as far as
        # the disk.
        if entry.path and entry.path.exists():
            if entry.removed:
                freed, error = _remove_tree(entry.path)
                entry.size_bytes = freed
                entry.error = error
            else:
                # A teardown that stopped halfway still freed whatever it got
                # through, and the summary adds up what every entry carries. The
                # scanned size is what this *would* have freed, so leaving it
                # here reports disk that is still occupied as reclaimed.
                entry.size_bytes = max(0, entry.size_bytes - _dir_size(entry.path))
        log.LOGGER(hypervisor.log(
            entry.vmachine_id,
            f"event=prune kind=runtime reason={entry.reason} registered={registered} "
            f"removed={entry.removed} freed_bytes={entry.size_bytes} "
            f"error={entry.error or 'none'}",
        ))
        return entry

    freed, error = _remove_tree(entry.path)
    entry.size_bytes = freed
    entry.error = error
    entry.removed = error is None
    log.LOGGER(
        f"[{FAMILY_LOG_TAG}][{entry.vmachine_id}] event=prune kind={entry.kind} "
        f"reason={entry.reason} removed={entry.removed} freed_bytes={freed} error={error or 'none'}"
    )
    return entry
