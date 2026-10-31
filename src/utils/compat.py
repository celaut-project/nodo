"""What a running node depends on, as a manifest that two versions can be compared by (#518).

``nodo update`` pulls new code only when nothing the installed node was built on has
changed. Three things qualify (the third is two entries, the database and ``storage/``):

* ``celaut_proto``: the hash of ``protos/celaut.proto``, the protocol. ``pack.proto`` and
  ``buffer.proto`` are not part of it.
* ``config_keys``: the key paths of ``config.example.yaml``. Values and comments change
  freely; a key added or removed means the operator's ``config.yaml`` may need a migration.
* ``database``: a hash of the schema, built from ``TABLES`` and the ``TRACEABILITY_*``
  definitions in an in-memory database, so it reads what SQLite will hold and not how the
  Python happens to spell it.
* ``storage_format``: ``STORAGE_FORMAT``, the layout of ``storage/``.

CI writes the result to ``compat.json`` (``python -m src.utils.compat --write``) and fails
when the file is behind the sources (``--check``), so a schema cannot change without the
change showing in the manifest.
"""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
from typing import Dict, List, Optional

import yaml

# The layout of ``storage/`` (``__block__``, ``__registry__``, ``__metadata__``). Raise it,
# by hand, in the commit that changes how any of them is laid out on disk.
STORAGE_FORMAT = 1

MANIFEST_FILE = "compat.json"
PROTO_FILE = os.path.join("protos", "celaut.proto")
CONFIG_EXAMPLE_FILE = "config.example.yaml"

# What a manifest compares by, and what the operator is told when each one differs.
ITEMS = {
    "celaut_proto": "protos/celaut.proto (the protocol)",
    "config_keys": "the keys of config.example.yaml (the config.yaml schema)",
    "database": "the database schema",
    "storage_format": "the layout of storage/",
}

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def proto_hash(root: str = ROOT) -> str:
    with open(os.path.join(root, PROTO_FILE), "rb") as f:
        # Line endings are not part of the protocol.
        return _sha256(f.read().replace(b"\r\n", b"\n"))


def flatten_keys(node, prefix: str = "") -> List[str]:
    """The dotted path of every leaf of a parsed YAML document, sorted."""
    if isinstance(node, dict) and node:
        paths: List[str] = []
        for key, value in node.items():
            paths.extend(flatten_keys(value, f"{prefix}.{key}" if prefix else str(key)))
        return sorted(paths)
    return [prefix] if prefix else []


def config_keys(root: str = ROOT) -> List[str]:
    with open(os.path.join(root, CONFIG_EXAMPLE_FILE), "r", encoding="utf-8") as f:
        return flatten_keys(yaml.safe_load(f) or {})


def config_keys_from_text(text: str) -> List[str]:
    return flatten_keys(yaml.safe_load(text) or {})


def database_hash() -> str:
    """Hash ``PRAGMA table_info`` of every table of a database made from the definitions."""
    from src.database.migrate import create_tables, ensure_columns, ensure_tables
    from src.database.sql_connection import TRACEABILITY_COLUMNS, TRACEABILITY_TABLES

    connection = sqlite3.connect(":memory:")
    try:
        cursor = connection.cursor()
        with contextlib.redirect_stdout(io.StringIO()):  # create_tables narrates every table.
            create_tables(cursor)
            ensure_tables(cursor, TRACEABILITY_TABLES)
            for table, columns in TRACEABILITY_COLUMNS.items():
                ensure_columns(cursor, table, columns)
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        schema = {}
        for (table,) in cursor.fetchall():
            cursor.execute(f"PRAGMA table_info({table})")
            schema[table] = [list(column) for column in cursor.fetchall()]
    finally:
        connection.close()
    return _sha256(json.dumps(schema, sort_keys=True).encode())


def compute_manifest(root: str = ROOT) -> Dict[str, object]:
    return {
        "celaut_proto": proto_hash(root),
        "config_keys": config_keys(root),
        "database": database_hash(),
        "storage_format": STORAGE_FORMAT,
    }


def dump_manifest(manifest: Dict[str, object]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def changed_items(installed: Dict[str, object], incoming: Dict[str, object]) -> List[str]:
    """The entries of ``ITEMS`` that differ between two manifests, in a fixed order."""
    return [key for key in ITEMS if installed.get(key) != incoming.get(key)]


def describe_change(item: str, installed: Dict[str, object], incoming: Dict[str, object]) -> str:
    line = f"  - {ITEMS[item]} changed"
    if item == "config_keys":
        before, after = set(installed.get(item) or []), set(incoming.get(item) or [])
        added, removed = sorted(after - before), sorted(before - after)
        if added:
            line += f"\n      added:   {', '.join(added)}"
        if removed:
            line += f"\n      removed: {', '.join(removed)}"
    return line


def read_manifest(text: Optional[str]) -> Optional[Dict[str, object]]:
    if text is None:
        return None
    try:
        manifest = json.loads(text)
    except ValueError:
        return None
    return manifest if isinstance(manifest, dict) else None


def main(argv: List[str]) -> int:
    root = ROOT
    path = os.path.join(root, MANIFEST_FILE)
    if len(argv) != 1 or argv[0] not in ("--write", "--check"):
        print("Usage: python -m src.utils.compat --write | --check", file=sys.stderr)
        return 2

    # Importing the database definitions reads the configuration; CI has none installed.
    from tests.config_bootstrap import load_example_config
    with contextlib.redirect_stdout(io.StringIO()):
        load_example_config()

    expected = dump_manifest(compute_manifest(root))
    if argv[0] == "--write":
        with open(path, "w", encoding="utf-8") as f:
            f.write(expected)
        print(f"Wrote {MANIFEST_FILE}.")
        return 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            current = f.read()
    except OSError:
        current = None
    if current == expected:
        print(f"{MANIFEST_FILE} is up to date.")
        return 0
    print(f"{MANIFEST_FILE} is out of date with the sources. Run: python -m src.utils.compat --write", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
