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


def logs(main_dir: str, argv=None) -> bool:
    from src.commands import _catalogue as catalogue

    argv = list(argv or [])
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
