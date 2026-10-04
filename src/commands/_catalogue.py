"""Read-only catalogue queries shared by the agent-facing CLI views.

These are the same questions ``nodo tui`` asks of the SQLite catalogue to draw its
detail cards (``src/commands/tui/src/app.rs``, ``peers.rs``, ``clients.rs``),
answered here so a script or an AI agent can ask them without a terminal. The SQL
is kept as close to the TUI's as Python allows, so the two readings of one row
cannot disagree.

Every function takes an open :class:`sqlite3.Connection` and degrades to an empty
answer on a table the database has not been migrated for -- a node with no
history is not an error, which is the line the TUI draws too.

Plus the two output helpers every ``--json`` command here uses: one JSON document
per invocation, on one line, so a caller reads exactly one object from stdout.
"""

import json
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

# How many payments / events / tokens a detail view returns by default. The TUI
# shows 8 (DETAIL_ROWS) because that is what fits a card; a script has no screen.
DEFAULT_DETAIL_ROWS = 50


def connect(database_file: str) -> sqlite3.Connection:
    connection = sqlite3.connect(database_file)
    connection.row_factory = sqlite3.Row
    return connection


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    # PRAGMA cannot be parameterised; every caller passes a literal.
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def payments(connection: sqlite3.Connection, column: str, value: str,
             limit: int = DEFAULT_DETAIL_ROWS) -> List[Dict[str, Any]]:
    """Payment rows for one counterparty, newest first (``get_payments`` in the TUI).

    ``column`` is ``peer_id`` or ``client_id`` and is interpolated -- both are
    literals at every call site; the value is bound.
    """
    if column not in ("peer_id", "client_id"):
        raise ValueError(f"unsupported payment column: {column}")
    if not table_exists(connection, "payments"):
        return []
    rows = connection.execute(
        f"SELECT created_at, direction, amount_mu, status, COALESCE(ledger, '') AS ledger, "
        f"COALESCE(tx_id, '') AS tx_id, COALESCE(deposit_token, '') AS deposit_token "
        f"FROM payments WHERE {column} = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (value, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def reputation_events(connection: sqlite3.Connection, kind: str, subject_id: str,
                      limit: int = DEFAULT_DETAIL_ROWS) -> List[Dict[str, Any]]:
    """Reputation events for one subject, newest first (``get_reputation_events``)."""
    if not table_exists(connection, "reputation_events"):
        return []
    rows = connection.execute(
        "SELECT created_at, amount, reason, score_after FROM reputation_events "
        "WHERE subject_kind = ? AND subject_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (kind, subject_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def take_limit(argv: List[str], default: int = DEFAULT_DETAIL_ROWS) -> int:
    """``--limit N`` from argv, or ``default``. Raises ValueError on a bad value."""
    for index, argument in enumerate(argv):
        if argument == "--limit":
            if index + 1 >= len(argv):
                raise ValueError("--limit needs a value: `--limit <rows>`.")
            value = argv[index + 1]
        elif argument.startswith("--limit="):
            value = argument.split("=", 1)[1]
        else:
            continue
        limit = int(value)
        if limit <= 0:
            raise ValueError("--limit must be a positive number of rows.")
        return limit
    return default


def positionals(argv: List[str], valued_flags=("--limit",)) -> List[str]:
    """argv without flags and the values of ``valued_flags``."""
    result, skip = [], False
    for argument in argv:
        if skip:
            skip = False
            continue
        if argument in valued_flags:
            skip = True
            continue
        if argument.startswith("-"):
            continue
        result.append(argument)
    return result


def emit_json(document: Dict[str, Any]) -> None:
    """One JSON object on one line, stamped with when it was read."""
    document.setdefault("read_at", int(time.time()))
    json.dump(document, sys.stdout, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_error(as_json: bool, message: str) -> bool:
    """Report a failure in the caller's chosen shape; always returns False."""
    if as_json:
        emit_json({"error": message})
    else:
        print(message, flush=True)
    return False


def mu_or_none(value: Any) -> Optional[int]:
    """An MU column (stored as TEXT) as an int, or None when it is not one."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
