"""``nodo prune`` -- reclaim the microVM cache disk nothing else reclaims.

`nodo remove <service>` frees the bundle of a service the operator names. Two
directories under ``CACHE/microvm/`` grow independently of any service:

* ``runtime/<vmachine_id>/`` -- an instance's own copy of its rootfs image. Freed
  by ``kill``, so it is only left behind when a teardown did not finish, or when
  a VM died without one.
* ``failures/<vmachine_id>/`` -- the runtime directory of a failed launch, kept
  on purpose for debugging (``virtualizers.ch.CONSERVE_RUNTIME_DIR_ON_FAILURE``)
  and, until now, kept forever. Gigabytes on a node that has been running a
  while.

This command reports both and, unless ``--dry-run``, removes them. It prints
every entry with its size and its reason -- including the ones it *keeps* and
why -- because an operator running this to find disk needs to see the whole
picture, not the subset this run happened to act on.

Reclaiming a ``runtime/`` entry can also stop the VM that owns it: an orphan is
a VM whose process may still be running (a guest whose kernel panicked is the
clearest case). Those are torn down through the same path the maintenance tick
uses, so an instance the database still has a row for is *stopped*, not merely
killed -- its row is purged and its unspent deposit goes back to its father.
This is disk reclamation that happens to end a VM's life, never a disk-only
sweep that leaves the books talking about something that no longer exists.
"""

import sys
from typing import List, Optional, Sequence, Tuple

from src.commands.inspect_service import format_size
from src.utils.privileges import refuse_unwritable
from src.utils.config import ConfigManager

env_manager = ConfigManager()

# How long a `failures/` entry is worth keeping for debugging. The default is a
# week: long enough that the launch failure an operator is investigating is still
# there tomorrow, short enough that it is not still there in a month.
DEFAULT_FAILURE_RETENTION_DAYS = 7


def _retention_days() -> float:
    raw = env_manager.get("virtualizers.ch.FAILURE_RETENTION_DAYS", DEFAULT_FAILURE_RETENTION_DAYS)
    try:
        days = float(raw)
    except (TypeError, ValueError):
        print(
            f"Warning: virtualizers.ch.FAILURE_RETENTION_DAYS is not a number ({raw!r}); "
            f"using {DEFAULT_FAILURE_RETENTION_DAYS} days."
        )
        return float(DEFAULT_FAILURE_RETENTION_DAYS)
    if days < 0:
        print(
            f"Warning: virtualizers.ch.FAILURE_RETENTION_DAYS is negative ({days}); "
            f"using {DEFAULT_FAILURE_RETENTION_DAYS} days."
        )
        return float(DEFAULT_FAILURE_RETENTION_DAYS)
    return days


def _format_age(age_seconds: Optional[float]) -> str:
    if age_seconds is None:
        return "age unknown"
    days = age_seconds / 86400.0
    if days >= 1:
        return f"{days:.1f}d old"
    hours = age_seconds / 3600.0
    if hours >= 1:
        return f"{hours:.1f}h old"
    return f"{age_seconds / 60.0:.0f}m old"


def _print_entries(title: str, entries: Sequence, *, verb: str) -> int:
    """Print one section and return the bytes it accounts for.

    Before a reclamation ``size_bytes`` is what the scan found on disk; after
    one it is what that entry actually freed, which is zero for an entry whose
    teardown failed. The same sum therefore reads as "would be freed" in a dry
    run and as "was freed" in a real one, and neither ever claims disk that is
    still there.
    """
    if not entries:
        return 0
    print(f"\n{title}")
    total = 0
    for entry in entries:
        total += entry.size_bytes
        suffix = ""
        if entry.error:
            suffix = f"  [{entry.error}]"
        elif verb == "removed" and not entry.removed:
            suffix = "  [not removed]"
        print(
            f"  {entry.vmachine_id[:32]:<34} {format_size(entry.size_bytes):>10}  "
            f"{entry.reason}, {_format_age(entry.age_seconds)}{suffix}"
        )
    return total


def _parse_args(argv: Sequence[str]) -> Tuple[bool, bool, List[str]]:
    args = list(argv)
    dry_run = "--dry-run" in args
    prune_all = "--all" in args
    unknown = [a for a in args if a not in ("--dry-run", "--all")]
    return dry_run, prune_all, unknown


def _scan_dead_instances(dry_run: bool, prune_all: bool) -> Tuple[List[str], List[str]]:
    """The dead local ids and dead delegated tokens that go. In a real run they are deleted.

    A failure reading the database is reported and counts as nothing to prune.
    """
    try:
        from src.database.sql_connection import SQLConnection
        from src.utils import dead_instances

        sc = SQLConnection()
        days = dead_instances.retention_days()
        rows = dead_instances.retention_max_rows()
        local = sc.prune_dead_local_instances(
            max_age_days=days, max_rows=rows, dry_run=dry_run, delete_all=prune_all
        )
        delegated = sc.prune_dead_delegated_instances(
            max_age_days=days, max_rows=rows, dry_run=dry_run, delete_all=prune_all
        )
        return local, delegated
    except Exception as e:
        print(f"Warning: could not prune the dead instances: {e}")
        return [], []


def prune(argv: Optional[Sequence[str]] = None) -> None:
    dry_run, prune_all, unknown = _parse_args(argv if argv is not None else sys.argv[2:])
    if unknown:
        print(f"Unknown argument(s): {' '.join(unknown)}")
        print("Usage: nodo prune [--all] [--dry-run]")
        return

    # Write access to the trees, not root: a partial removal would leave a
    # half-freed directory and report a number that was never true.
    if not dry_run:
        from src.virtualizers.microvm import paths

        if refuse_unwritable([str(paths.runtime_root()), str(paths.failures_root())]):
            return

    # Read from the microVM family rather than through `virtualizers.interface`,
    # deliberately: what this command reports is a disk layout -- runtime
    # directories, rootfs images, preserved failures, and their sizes -- and that
    # is the family's, not something every backend has. A family with nothing on
    # this node's disk has nothing to report here. See docs/BACKENDS.md and #290.
    try:
        from src.virtualizers.microvm.maintain import (
            reclaim,
            scan_failures,
            scan_orphan_runtimes,
        )
    except Exception as e:
        print(f"Could not load the cache maintenance helpers: {e}")
        return

    retention_seconds = None if prune_all else _retention_days() * 86400.0

    try:
        runtimes = scan_orphan_runtimes()
    except Exception as e:
        print(f"Warning: could not scan runtime directories: {e}")
        runtimes = []

    try:
        failures, kept_failures = scan_failures(retention_seconds=retention_seconds)
    except Exception as e:
        print(f"Warning: could not scan the failures directory: {e}")
        failures, kept_failures = [], []

    reclaimable = list(runtimes) + list(failures)

    # Dead instances: the same limits as the node's own hourly sweep, or all of them
    # with --all. A dry run only counts them.
    dead_local, dead_delegated = _scan_dead_instances(dry_run, prune_all)
    dead_count = len(dead_local) + len(dead_delegated)

    if not reclaimable and not kept_failures and not dead_count:
        print(
            "Nothing to prune: no orphaned runtime directories, no preserved failures "
            "and no old dead instances."
        )
        return

    if dry_run:
        total = 0
        total += _print_entries(
            "Orphaned runtime directories (would be removed):", runtimes, verb="would remove"
        )
        total += _print_entries(
            "Preserved launch failures (would be removed):", failures, verb="would remove"
        )
        _print_entries("Kept:", kept_failures, verb="kept")
        if dead_count:
            print(f"\nDead instances ({dead_count} would be deleted with their saved logs).")
        print(f"\nDry run: {format_size(total)} would be freed. Nothing was removed.")
        return

    for entry in reclaimable:
        try:
            reclaim(entry)
        except Exception as e:
            entry.error = str(e)
            entry.removed = False
            # A reclamation that did not even get to report itself freed
            # nothing anyone can rely on; the total below only adds up disk
            # that was measured after the fact.
            entry.size_bytes = 0

    if dead_count:
        from src.utils import dead_instances

        dead_instances.remove_dirs(dead_local)
        print(f"\nDead instances: deleted {dead_count} with their saved logs.")

    freed_runtimes = _print_entries("Orphaned runtime directories:", runtimes, verb="removed")
    freed_failures = _print_entries("Preserved launch failures:", failures, verb="removed")
    _print_entries("Kept:", kept_failures, verb="kept")

    failed = [e for e in reclaimable if not e.removed]
    total = freed_runtimes + freed_failures
    print(f"\nFreed {format_size(total)} in total.")
    if failed:
        print(
            f"{len(failed)} entr{'y' if len(failed) == 1 else 'ies'} could not be fully removed; "
            "see the reasons above."
        )
    if kept_failures and not prune_all:
        kept_bytes = sum(e.size_bytes for e in kept_failures)
        print(
            f"{format_size(kept_bytes)} of recent launch failures were kept for debugging; "
            "run with --all to remove them too."
        )
