"""``nodo burnall`` -- stop every instance this node is running.

``nodo kill <id>`` stops one instance the operator names. There is no way to
name all of them, and the situations that call for it are the ones where the
operator does not know the list: a test run that leaked children, a node to be
handed back clean, a machine whose cores are pinned by work nobody asked for.
Reading ``nodo instances`` and pasting ids back one at a time is that list,
assembled by hand, with a race in the middle -- see the ordering note below.

Parents go first, and that is the whole reason this is a command rather than a
shell loop. A running orchestrator launches children on demand, so killing its
children while it is still up means killing instances it is busy replacing. By
the time the loop reaches the parent, the node is running services the loop
already stopped once. Ordering by depth in the parent tree, roots first, means
nothing outlives the thing that would ask for it again.

Every instance is still stopped through ``stop_instance``, exactly as ``kill``
does: the deposit is refunded to whoever paid it in, the memory is released and
the row is purged. This is bulk, not a shortcut.
"""

import os
import sys
from typing import Dict, List, Sequence

from src.database.sql_connection import SQLConnection
from src.manager.manager import stop_instance

sc = SQLConnection()


def _depth(vmachine_id: str, fathers: Dict[str, str], internal: Sequence[str]) -> int:
    """How many internal ancestors an instance has; 0 when its parent is a client.

    A father that is not itself an internal instance is a client, which ends the
    chain. The visited set is not defensive dressing: a father cycle would
    otherwise spin here forever, and this runs before anything has been stopped.
    """
    known = set(internal)
    seen = set()
    depth, current = 0, vmachine_id
    while True:
        father = fathers.get(current) or ""
        if not father or father not in known or father in seen:
            return depth
        seen.add(current)
        current, depth = father, depth + 1


def burnall(argv: List[str] = None) -> None:
    argv = list(argv or [])
    dry_run = "--dry-run" in argv
    unknown = [a for a in argv if a != "--dry-run"]
    if unknown:
        print(f"burnall: unrecognised argument(s): {' '.join(unknown)}")
        print("usage: nodo burnall [--dry-run]")
        return

    # Only the dry run is readable without privileges; stopping needs the same
    # rights `kill` needs, and finding that out after printing a plan is worse
    # than finding it out now.
    if not dry_run and os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return

    try:
        ids = sc.get_all_internal_containers_ids() or []
    except Exception as e:
        print(f"burnall: could not read the instance list: {e}")
        return

    if not ids:
        print("No instances running.")
        return

    fathers = {}
    for i in ids:
        try:
            fathers[i] = sc.get_internal_father_id(id=i) or ""
        except Exception:
            fathers[i] = ""

    ordered = sorted(ids, key=lambda i: _depth(i, fathers, ids))

    print(f"{len(ordered)} instance(s), parents first:")
    for i in ordered:
        depth = _depth(i, fathers, ids)
        print(f"  {'  ' * depth}{i[:16]}  (depth {depth})")

    if dry_run:
        print("\n--dry-run: nothing stopped.")
        return

    stopped, failed = 0, []
    for i in ordered:
        # One instance that will not stop must not strand the rest: the ones
        # after it in the order are its children, and they are exactly what a
        # burnall is for.
        try:
            if stop_instance(token=i) is not None:
                stopped += 1
            else:
                failed.append((i, "stop_instance reported no result"))
        except Exception as e:
            failed.append((i, str(e)))

    print(f"\nStopped {stopped} of {len(ordered)} instance(s).")
    for i, err in failed:
        print(f"  failed: {i[:16]} -- {err}")
    if failed:
        # Children of a parent that would not stop may have been relaunched
        # while this ran, so the count above is not a guarantee of an empty node.
        print("\nRe-run `nodo instances` to confirm what is left.")
