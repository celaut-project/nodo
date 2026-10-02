"""``nodo tunnels`` and ``nodo tunnel_close`` -- the tunnels running on this host.

``nodo tunnel`` opens one; these find and stop them again, from any shell, a script
or the TUI's TUNNELS page, which reads the same registry
(``src/utils/tunnel_registry.py``). Only the client ends this host opened are here:
the node keeps no table of the streams it relays for others (``docs/TUNNELING.md``).
"""

import time
from typing import Any, Dict, List

from src.commands._catalogue import emit_error, emit_json
from src.utils import tunnel_registry as registry

#: Log lines an inspect shows of a detached tunnel.
LOG_TAIL_LINES = 20


def _age(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "?"
    seconds = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _listen(record: Dict[str, Any]) -> str:
    return f"{record['listen_host']}:{record['listen_port']}/{record['transport']}"


def render_list(tunnels: List[Dict[str, Any]]) -> str:
    """The text table. Pure, so the wording is testable without a process."""
    if not tunnels:
        return "No tunnels running. Open one with `nodo tunnel <instance> <slot> --detach`.\n"
    header = ("ID", "LISTEN", "SLOT", "INSTANCE", "VIA", "PID", "AGE")
    rows = [
        (
            record["id"],
            _listen(record),
            str(record["slot"]),
            record["instance"],
            record["peer"] or "this node",
            str(record["pid"]),
            _age(record.get("age_secs")),
        )
        for record in tunnels
    ]
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
             for row in [header, *rows]]
    return "\n".join(lines) + "\n"


def render_one(record: Dict[str, Any], log_lines: List[str]) -> str:
    started = record.get("started_at")
    started_text = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
        if isinstance(started, (int, float)) else "?"
    )
    lines = [
        f"Tunnel {record['id']}",
        f"  listening   {_listen(record)}",
        f"  reaches     slot {record['slot']} of {record['token']}",
        f"  instance    {record['instance']}",
        f"  via         {record['gateway']}" + ("" if record.get("peer") else " (this node)"),
        f"  pid         {record['pid']}" + (" (detached)" if record.get("detached") else ""),
        f"  started     {started_text} ({_age(record.get('age_secs'))} ago)",
    ]
    if record.get("log"):
        lines.append(f"  log         {record['log']}")
    if log_lines:
        lines.append("")
        lines.append("Last log lines:")
        lines.extend(f"  {line}" for line in log_lines)
    return "\n".join(lines) + "\n"


def _one(reference: str, as_json: bool):
    """The tunnel ``reference`` names, or an error already reported."""
    matches = registry.find(reference)
    if not matches:
        return None, emit_error(as_json, f"No running tunnel '{reference}'. See `nodo tunnels`.")
    if len(matches) > 1:
        ids = ", ".join(record["id"] for record in matches)
        return None, emit_error(as_json, f"'{reference}' matches several tunnels: {ids}.")
    return matches[0], True


def list_tunnels(reference: str = "", as_json: bool = False) -> bool:
    """No reference: every running tunnel. A tunnel id (or prefix): that one."""
    if reference:
        record, ok = _one(reference, as_json)
        if record is None:
            return ok
        log_lines = registry.log_tail(record, LOG_TAIL_LINES)
        if as_json:
            emit_json({"tunnel": {**record, "log_tail": log_lines}})
        else:
            print(render_one(record, log_lines), end="", flush=True)
        return True

    tunnels = registry.list_tunnels()
    if as_json:
        emit_json({"tunnels": tunnels})
    else:
        print(render_list(tunnels), end="", flush=True)
    return True


def close_tunnels(references: List[str], close_all: bool = False, as_json: bool = False) -> bool:
    """Stop the named tunnels (or every one, with ``--all``).

    Exit status 1 if any named tunnel was not found or could not be signalled; the
    ones that could be closed are closed regardless.
    """
    if close_all:
        targets = registry.list_tunnels()
    else:
        if not references:
            return emit_error(as_json, "Usage: nodo tunnel_close <tunnel id>... | --all")
        targets = []
        for reference in references:
            record, ok = _one(reference, as_json)
            if record is None:
                return ok
            targets.append(record)

    closed, failed = [], []
    for record in targets:
        (closed if registry.close(record) else failed).append(record["id"])

    if as_json:
        document: Dict[str, Any] = {"closed": closed, "failed": failed}
        if failed:
            document["error"] = "permission denied: the tunnel belongs to another user"
        emit_json(document)
    else:
        if not targets:
            print("No tunnels running.", flush=True)
        for tunnel_id in closed:
            print(f"Closed tunnel {tunnel_id}.", flush=True)
        for tunnel_id in failed:
            print(f"Could not close tunnel {tunnel_id}: it belongs to another user "
                  "(try sudo).", flush=True)
    return not failed
