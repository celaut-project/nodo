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

import os
import sys
from typing import List, Optional, Sequence, Tuple

from src.commands.inspect_service import format_size
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


def prune(argv: Optional[Sequence[str]] = None) -> None:
    dry_run, prune_all, unknown = _parse_args(argv if argv is not None else sys.argv[2:])
    if unknown:
        print(f"Unknown argument(s): {' '.join(unknown)}")
        print("Usage: nodo prune [--all] [--dry-run]")
        return

    # Root, like `remove` and `kill`: these trees are written by a node running as
    # root, and a partial removal by an unprivileged user would leave a half-freed
    # directory and report a number that was never true.
    if not dry_run and os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
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

    if not reclaimable and not kept_failures:
        print("Nothing to prune: no orphaned runtime directories and no preserved failures.")
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
        print(f"\nDry run: {format_size(total)} would be freed. Nothing was removed.")
        return

    for entry in reclaimable:
        try:
            reclaim(entry)
        except Exception as e:
            entry.error = str(e)
            entry.removed = False

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
