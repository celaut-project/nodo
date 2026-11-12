"""Which instance holds which physical device: the exclusive assignment table (#536).

One row per PCI address (the primary key), so two instances can never hold one
device: a second reservation of an address fails in the database, whatever the
threads or processes that race for it. A row lives from the reservation before the
virtual machine starts until the node releases it (stop, kill, hibernate, a failed
start). A wake reserves again: the instance may get another device of the same
architecture.
"""
import sqlite3
import time
from typing import Dict, List, Sequence

from src.devices.spec import NodeDevice

TABLE = "device_assignments"


def _db():
    from src.database.sql_connection import SQLConnection

    return SQLConnection()


def assigned() -> Dict[str, str]:
    """``{address: instance id}`` for every held device."""
    try:
        rows = _db()._execute(f"SELECT address, instance_id FROM {TABLE}").fetchall()
    except sqlite3.Error:
        return {}
    return {row[0]: row[1] for row in rows}


def of_instance(instance_id: str) -> List[dict]:
    try:
        rows = _db()._execute(
            f"SELECT address, chip, kernel_hash, assigned_at FROM {TABLE} "
            f"WHERE instance_id = ? ORDER BY address",
            (instance_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"address": r[0], "chip": r[1], "kernel_hash": r[2], "assigned_at": r[3]}
        for r in rows
    ]


def reserve(instance_id: str, entries: Sequence[NodeDevice]) -> bool:
    """Hold all ``entries`` for ``instance_id`` in one transaction, or none.

    False when another instance holds one of the addresses.
    """
    if not entries:
        return True
    now = int(time.time())
    queries = [
        (
            f"INSERT INTO {TABLE} (address, instance_id, chip, kernel_hash, assigned_at) "
            f"VALUES (?, ?, ?, ?, ?)",
            (e.address, instance_id, e.chip, e.kernel_hash, now),
        )
        for e in entries
    ]
    try:
        _db()._execute2(queries)
    except sqlite3.IntegrityError:
        return False
    return True


def release(instance_id: str) -> List[str]:
    """Free every device of ``instance_id``. Returns the addresses freed."""
    held = [d["address"] for d in of_instance(instance_id)]
    if held:
        _db()._execute(f"DELETE FROM {TABLE} WHERE instance_id = ?", (instance_id,))
    return held
