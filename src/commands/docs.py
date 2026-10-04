"""``nodo docs [<page>] [--json]`` -- the DOCS page of ``nodo tui``, non-interactively.

The installation's ``docs/`` folder, read in place so it describes the version
that is installed. No argument lists every Markdown page (its path under
``docs/`` and its first ``#`` heading); a page name -- ``CONFIG``, ``CONFIG.md``
or ``proposals/x.md`` -- prints that page's Markdown. ``NODO_DOCS_DIR`` names
the folder explicitly, as it does for the TUI.
"""

import os
from typing import Dict, List, Optional


def docs_dir(main_dir: str) -> str:
    return os.environ.get("NODO_DOCS_DIR") or os.path.join(main_dir, "docs")


def _title(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if line.startswith("#"):
                    return line.lstrip("#").strip()
    except OSError:
        pass
    return ""


def pages(root: str) -> List[Dict[str, str]]:
    found = []
    for dirpath, dirnames, names in os.walk(root):
        dirnames.sort()
        for name in sorted(names):
            if name.lower().endswith(".md"):
                full = os.path.join(dirpath, name)
                found.append({"page": os.path.relpath(full, root), "title": _title(full)})
    return found


def resolve(root: str, page: str) -> Optional[str]:
    candidates = [page, page + ".md", page.upper() + ".md"]
    for candidate in candidates:
        full = os.path.realpath(os.path.join(root, candidate))
        # Never outside docs/: a page name is not a path to anywhere on disk.
        if full.startswith(os.path.realpath(root) + os.sep) and os.path.isfile(full):
            return full
    return None


def docs(main_dir: str, argv=None) -> bool:
    from src.commands import _catalogue as catalogue

    argv = list(argv or [])
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    root = docs_dir(main_dir)
    if not os.path.isdir(root):
        return catalogue.emit_error(as_json, f"No docs folder at {root} (set NODO_DOCS_DIR).")
    if not args:
        listing = pages(root)
        if as_json:
            catalogue.emit_json({"root": root, "pages": listing})
        else:
            width = max((len(p["page"]) for p in listing), default=0) + 2
            for entry in listing:
                print(f"{entry['page'].ljust(width)}{entry['title']}")
        return True
    path = resolve(root, args[0])
    if not path:
        return catalogue.emit_error(as_json, f"No docs page {args[0]!r}; run `nodo docs` for the list.")
    with open(path, errors="replace") as f:
        text = f.read()
    if as_json:
        catalogue.emit_json({"page": os.path.relpath(path, root), "title": _title(path), "markdown": text})
    else:
        print(text, end="" if text.endswith("\n") else "\n", flush=True)
    return True
