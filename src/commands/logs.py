"""``nodo logs`` -- this node's log, followed or as a bounded tail.

Bare ``nodo logs`` follows ``storage/app.log`` forever (``tail -f``), which is what
a person at a terminal wants and what a script or an AI agent cannot use: it never
exits. ``-n``/``--lines N`` prints the last N lines and exits -- the LOGS page of
``nodo tui``, as text or, with ``--json``, as ``{"path", "lines": [...]}``.
"""

import os
from collections import deque
from typing import List, Optional


def tail(path: str, count: int) -> List[str]:
    with open(path, "r", errors="replace") as f:
        return [line.rstrip("\n") for line in deque(f, maxlen=count)]


def _lines_option(argv: List[str]) -> Optional[int]:
    for index, argument in enumerate(argv):
        if argument in ("-n", "--lines"):
            if index + 1 >= len(argv):
                raise ValueError(f"{argument} needs a number of lines.")
            text = argv[index + 1]
        elif argument.startswith("--lines="):
            text = argument.split("=", 1)[1]
        else:
            continue
        count = int(text)
        if count <= 0:
            raise ValueError("The number of lines must be positive.")
        return count
    return None


def _instance_option(argv: List[str]) -> Optional[str]:
    """The instance reference, if ``argv`` has one: a word that is not an option or its value."""
    skip = False
    for argument in argv:
        if skip:
            skip = False
        elif argument in ("-n", "--lines", "--stream"):
            skip = True
        elif not argument.startswith("-"):
            return argument
    return None


def _stream_option(argv: List[str]) -> Optional[str]:
    for index, argument in enumerate(argv):
        if argument == "--stream" and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith("--stream="):
            return argument.split("=", 1)[1]
    return None


def dead_instance_logs(reference: str, argv: List[str]) -> bool:
    """``nodo logs <instance> [--stream stdout|stderr|serial] [-n N] [--json]``.

    Show the logs saved when a local instance died. A live instance has no archive.
    """
    import sqlite3

    from src.commands import _catalogue as catalogue
    from src.utils import dead_instances
    from src.utils.config import ConfigManager

    as_json = "--json" in argv
    connection = sqlite3.connect(f"file:{ConfigManager().get('DATABASE_FILE')}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT id, name, killed_at, death_reason FROM local_instances "
            "WHERE killed_at IS NOT NULL AND (id = ? OR name = ?) "
            "ORDER BY killed_at DESC LIMIT 1",
            (reference, reference),
        ).fetchone()
        delegated = None
        if row is None:
            delegated = connection.execute(
                "SELECT id, killed_at, death_reason FROM delegated_instances "
                "WHERE killed_at IS NOT NULL AND (id = ? OR token_delegation = ?) LIMIT 1",
                (reference, reference),
            ).fetchone()
    except sqlite3.Error as e:
        return catalogue.emit_error(as_json, f"Could not read the database: {e}")
    finally:
        connection.close()
    if row is None and delegated is not None:
        return catalogue.emit_error(
            as_json,
            f"{reference} was a delegated instance. It died at {delegated['killed_at']} "
            f"({delegated['death_reason']}). Its logs are on the peer.",
        )
    if row is None:
        return catalogue.emit_error(as_json, f"No dead instance matches '{reference}'.")
    available = dead_instances.list_logs(row["id"])
    if not available:
        return catalogue.emit_error(as_json, f"No saved logs for {row['id']}.")
    stream = _stream_option(argv)
    if stream and stream not in available:
        return catalogue.emit_error(
            as_json, f"No '{stream}' log for {row['id']}. Available: {', '.join(sorted(available))}."
        )
    try:
        count = _lines_option(argv)
    except ValueError as e:
        return catalogue.emit_error(as_json, str(e))
    selected = [stream] if stream else sorted(available)
    result = {}
    for name in selected:
        try:
            result[name] = tail(str(available[name]), count or 200)
        except OSError as e:
            return catalogue.emit_error(as_json, f"Could not read {available[name]}: {e}")
    if as_json:
        catalogue.emit_json({
            "id": row["id"], "killed_at": row["killed_at"],
            "death_reason": row["death_reason"], "logs": result,
        })
        return True
    print(f"Instance {row['id']} died at {row['killed_at']} ({row['death_reason']}).")
    for name in selected:
        print(f"--- {name} ---")
        print("\n".join(result[name]), flush=True)
    return True


def logs(main_dir: str, argv=None) -> bool:
    from src.commands import _catalogue as catalogue

    argv = list(argv or [])
    reference = _instance_option(argv)
    if reference:
        return dead_instance_logs(reference, argv)
    as_json = "--json" in argv
    path = os.path.join(main_dir, "storage", "app.log")
    try:
        count = _lines_option(argv)
    except ValueError as e:
        return catalogue.emit_error(as_json, f"{e} Usage: nodo logs [-n <lines>] [--json]")
    if count is None and not as_json:
        return os.system(f"tail -f {path}") == 0
    try:
        lines = tail(path, count or 200)
    except OSError as e:
        return catalogue.emit_error(as_json, f"Could not read {path}: {e}")
    if as_json:
        catalogue.emit_json({"path": path, "lines": lines})
    else:
        print("\n".join(lines), flush=True)
    return True
