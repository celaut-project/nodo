"""``nodo pack``, ``nodo packs`` and ``nodo pack_cancel`` -- packing, as processes to watch.

``nodo pack <dir | https git URL>`` packs in the foreground as it always has; with
``--detach`` it runs in the background and returns at once with the pack's id, which
is the form for scripts, agents and the TUI's PACKS page (none of which can keep a
terminal open for a build that takes minutes). Either way every pack is recorded in
``<main.STORAGE>/packs`` (``src/utils/pack_registry.py``), so ``nodo packs`` lists the
current and recent ones from any shell, and ``nodo pack_cancel`` stops one.
"""

import os
import sys
import time
from typing import Any, Dict, List, Optional

from src.commands._catalogue import emit_error, emit_json
from src.utils import pack_registry as registry

#: Log lines an inspect shows.
LOG_TAIL_LINES = 20

USAGE = "Usage: nodo pack <project directory | https git URL[#subdir]> [--local] [--detach] [--json]"

NODO_PY = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "nodo.py"))


def _age(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "?"
    seconds = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _packer_kind(local: bool = False) -> str:
    if local:
        return "local"
    try:
        from src.utils.config import ConfigManager

        return "local" if ConfigManager().get("packer.local", False) else "service"
    except Exception:
        return "service"


def outcome(record: Dict[str, Any]) -> str:
    """What a pack came to, or what it is doing: one cell of the table."""
    status = record.get("status")
    if status == registry.DONE:
        return record.get("service_id") or "?"
    if status in registry.ACTIVE:
        return record.get("stage") or status
    return record.get("error") or status or "?"


def render_list(packs: List[Dict[str, Any]]) -> str:
    """The text table. Pure, so the wording is testable without a process."""
    if not packs:
        return "No packs on record. Start one with `nodo pack <dir | https git URL> --detach`.\n"
    header = ("ID", "STATUS", "SOURCE", "RESULT / STAGE", "AGE", "TOOK")
    rows = []
    for record in packs:
        source = str(record.get("source") or "?")
        if len(source) > 48:
            source = "…" + source[-47:]
        result = outcome(record)
        if len(result) > 66:
            result = result[:65] + "…"
        rows.append((
            record["id"],
            str(record.get("status") or "?"),
            source,
            result,
            _age(record.get("age_secs")),
            _age(record.get("duration_secs")),
        ))
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
             for row in [header, *rows]]
    return "\n".join(lines) + "\n"


def render_one(record: Dict[str, Any], log_lines: List[str]) -> str:
    def when(stamp):
        return (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
                if isinstance(stamp, (int, float)) else "-")

    lines = [
        f"Pack {record['id']}",
        f"  status      {record.get('status')}"
        + (f" ({record['stage']})" if record.get("stage") else ""),
        f"  source      {record.get('source')} ({record.get('kind')})",
        f"  packer      {record.get('packer')}",
        f"  pid         {record.get('pid')}" + (" (detached)" if record.get("detached") else ""),
        f"  started     {when(record.get('started_at'))} ({_age(record.get('age_secs'))} ago)",
        f"  finished    {when(record.get('finished_at'))}"
        + (f" (took {_age(record.get('duration_secs'))})" if record.get("finished_at") else ""),
    ]
    if record.get("service_id"):
        lines.append(f"  service id  {record['service_id']}")
    if record.get("error"):
        lines.append(f"  error       {record['error']}")
    if record.get("log"):
        lines.append(f"  log         {record['log']}")
    if log_lines:
        lines.append("")
        lines.append("Last log lines:")
        lines.extend(f"  {line}" for line in log_lines)
    return "\n".join(lines) + "\n"


# -- nodo pack -----------------------------------------------------------------------------


def pack_command(argv: List[str]) -> int:
    """``nodo pack <source> [--local] [--detach] [--json]``; returns the exit status.

    1 when the source is refused or the pack produced no service (it used to exit 0
    either way, which a script could not tell from success).
    """
    args = list(argv)
    as_json = "--json" in args and not args.remove("--json")
    detached = "--detach" in args and not args.remove("--detach")
    local = "--local" in args and not args.remove("--local")
    if len(args) != 1 or args[0].startswith("--"):
        return 0 if emit_error(as_json, USAGE) else 1

    try:
        kind, source = registry.validate_source(
            args[0], base_dir=os.environ.get("ORIGINAL_DIR", os.getcwd())
        )
    except ValueError as e:
        emit_error(as_json, f"Error: {e}")
        return 1

    if detached:
        return 0 if detach(source, as_json=as_json, local=local) else 1
    return 0 if foreground(source, kind, as_json=as_json, local=local) else 1


def detach(source: str, as_json: bool = False, timeout_s: float = registry.DETACH_TIMEOUT_S,
           command: Optional[List[str]] = None, local: bool = False) -> bool:
    record, error = registry.spawn_detached(source, NODO_PY, timeout_s=timeout_s, command=command,
                                            options=["--local"] if local else None)
    if record is None:
        return emit_error(as_json, error)
    if as_json:
        emit_json({"pack": record})
    else:
        print(f"Packing in the background: pack {record['id']} (pid {record['pid']}).", flush=True)
        print(f"  source  {record['source']}", flush=True)
        print(f"  log     {record['log']}", flush=True)
        print(f"Follow it with `nodo packs {record['id']}`; stop it with "
              f"`nodo pack_cancel {record['id']}`.", flush=True)
    return True


def foreground(source: str, kind: str, as_json: bool = False, local: bool = False) -> bool:
    """Pack here, registered so `nodo packs` and the TUI see it while it runs.

    With ``--json`` the packer's chatter goes to stderr and stdout carries only the
    final record.
    """
    from src.commands.packer.zip_with_dockerfile.pack import pack

    saved_stdout = None
    if as_json:
        sys.stdout.flush()
        saved_stdout = os.dup(1)
        os.dup2(2, 1)
    try:
        service_id, pack_id = registry.run(
            source, kind, _packer_kind(local), lambda: pack(directory=source, local=local)
        )
    finally:
        if saved_stdout is not None:
            sys.stdout.flush()
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)

    if as_json:
        found = registry.find(pack_id)
        if found:
            emit_json({"pack": found[0]})
        elif service_id:
            emit_json({"pack": {"id": pack_id, "status": registry.DONE, "service_id": service_id}})
        else:
            emit_json({"error": "packing produced no service id"})
    return bool(service_id)


# -- nodo packs / pack_cancel -------------------------------------------------------------


def _one(reference: str, as_json: bool):
    matches = registry.find(reference)
    if not matches:
        return None, emit_error(as_json, f"No pack '{reference}'. See `nodo packs`.")
    if len(matches) > 1:
        ids = ", ".join(record["id"] for record in matches)
        return None, emit_error(as_json, f"'{reference}' matches several packs: {ids}.")
    return matches[0], True


def list_packs(reference: str = "", as_json: bool = False, active_only: bool = False) -> bool:
    """No reference: every pack on record, newest first. A pack id (or prefix): that one,
    with the tail of its log."""
    if reference:
        record, ok = _one(reference, as_json)
        if record is None:
            return ok
        log_lines = registry.log_tail(record, LOG_TAIL_LINES)
        if as_json:
            emit_json({"pack": {**record, "log_tail": log_lines}})
        else:
            print(render_one(record, log_lines), end="", flush=True)
        return True

    packs = registry.list_packs()
    if active_only:
        packs = [record for record in packs if record.get("status") in registry.ACTIVE]
    if as_json:
        emit_json({"packs": packs})
    elif active_only and not packs:
        print("No pack is running.", flush=True)
    else:
        print(render_list(packs), end="", flush=True)
    return True


def cancel_packs(references: List[str], as_json: bool = False,
                 grace_s: float = registry.CANCEL_GRACE_S) -> bool:
    """Stop the named packs. Exit 1 if any was not found, not running, or not ours."""
    if not references:
        return emit_error(as_json, "Usage: nodo pack_cancel <pack id>... [--json]")
    targets = []
    for reference in references:
        record, ok = _one(reference, as_json)
        if record is None:
            return ok
        targets.append(record)

    cancelled, failed, messages = [], [], []
    for record in targets:
        ok, message = registry.cancel(record, grace_s=grace_s)
        (cancelled if ok else failed).append(record["id"])
        messages.append(message)

    if as_json:
        document: Dict[str, Any] = {"cancelled": cancelled, "failed": failed}
        if failed:
            document["error"] = "; ".join(
                message for record, message in zip(targets, messages) if record["id"] in failed
            )
        emit_json(document)
    else:
        for message in messages:
            print(message, flush=True)
    return not failed
