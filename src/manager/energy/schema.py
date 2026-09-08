"""Idempotent energy-table reshape.

The original ``energy_consumption`` stored a precomputed ``cost`` next to a
rough ``power_consumption`` estimate, and ``monitoring_config`` was never
written. Issue #258 replaces both. The node is not in production, so old rows
are dropped rather than migrated.

``CREATE TABLE IF NOT EXISTS`` will not change columns on an existing table, so
an upgrade that only creates-if-missing would keep the dead schema. This
reshape inspects columns and drops the old table when it is the pre-258 shape.

Pure sqlite3 — no ConfigManager — so tests do not need the rest of nodo.
"""

from __future__ import annotations

ENERGY_CONSUMPTION_SQL = """
CREATE TABLE IF NOT EXISTS energy_consumption (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME,
    energy_joules REAL,
    watts REAL,
    price_per_kwh REAL,
    currency TEXT,
    backend TEXT,
    is_floor INTEGER
)
"""

INSTANCE_ENERGY_SQL = """
CREATE TABLE IF NOT EXISTS instance_energy (
    instance_id TEXT PRIMARY KEY,
    watts REAL,
    share REAL,
    sample_count INTEGER,
    last_refresh DATETIME,
    FOREIGN KEY (instance_id) REFERENCES local_instances (id)
)
"""


def _columns(cursor, table: str) -> set:
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _table_exists(cursor, table: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cursor.fetchone() is not None


def energy_consumption_is_legacy(cursor) -> bool:
    """True when the table exists in the pre-258 shape (or a broken hybrid)."""
    if not _table_exists(cursor, "energy_consumption"):
        return False
    columns = _columns(cursor, "energy_consumption")
    if "energy_joules" not in columns:
        return True
    if "power_consumption" in columns or "cost" in columns:
        return True
    return False


def reshape_energy_schema(cursor) -> None:
    """Drop dead tables / legacy columns; ensure the #258 schema exists.

    Idempotent. Safe to run on every startup.
    """
    if _table_exists(cursor, "monitoring_config"):
        cursor.execute("DROP TABLE monitoring_config")

    if energy_consumption_is_legacy(cursor):
        cursor.execute("DROP TABLE energy_consumption")

    cursor.execute(ENERGY_CONSUMPTION_SQL)
    cursor.execute(INSTANCE_ENERGY_SQL)
