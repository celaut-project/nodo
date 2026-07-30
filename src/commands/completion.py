"""Shell completion for the nodo CLI.

This module powers ``<Tab>`` completion of nodo commands and of the service /
instance / peer identifiers (and service tags) that many commands accept.

It is intentionally lightweight: importing it must **not** pull in the
gateway / grpc / virtualizer graph, because the generated shell scripts call it
on every keypress. The only non-stdlib imports (``ConfigManager`` and the
protobuf ``Metadata`` message) are deferred until a dynamic candidate list is
actually requested, and are cheap relative to the full ``nodo.py`` import chain.

Two entry points:

* ``python src/commands/completion.py list <kind>`` — print candidates, one per
  line. Used directly by the bash/zsh scripts for low latency.
* ``nodo completion <bash|zsh|install>`` — print or install the shell scripts.
"""

import os
import sqlite3
import sys
from typing import Dict, List, Optional

# --- Command catalogue -------------------------------------------------------
# Keep this in sync with the ``match sys.argv[1]`` dispatch in nodo.py.

# Commands whose first positional argument is a service id or tag.
SERVICE_COMMANDS = [
    "execute",
    "estimate",
    "inspect",
    "remove",
    "publish",
    "tag",
    "export",
    "integrity",
]

# Commands whose first positional argument is an instance id.
INSTANCE_COMMANDS = ["kill", "observe", "increase_gas", "decrease_gas"]

# Commands whose first positional argument is a peer id.
PEER_COMMANDS = ["disconnect", "increase_peer_deposit", "verify_reputation", "pay_and_verify"]

# Commands whose first positional argument is a filesystem path (a project dir,
# a .bee file, a config dir, …). These get file/dir completion, not an id list.
PATH_COMMANDS = ["import", "pack", "ggconf"]

# Commands whose SECOND positional argument is a filesystem path
# (e.g. `export <service> <path>`). The first arg is still an id.
SECOND_ARG_PATH_COMMANDS = ["export"]

DAEMON_SUBCOMMANDS = ["start", "status", "stop", "restart"]

# Every top-level command nodo understands.
COMMANDS = sorted(
    {
        "help",
        "info",
        "logs",
        "export",
        "import",
        "publish",
        "download",
        "integrity",
        "execute",
        "estimate",
        "update",
        "kill",
        "observe",
        "increase_gas",
        "decrease_gas",
        "remove",
        "inspect",
        "services",
        "tag",
        "clients",
        "peers",
        "instances",
        "connect",
        "disconnect",
        "submit_reputation",
        "sync_reputation_proof",
        "refresh_ergo_nodes",
        "serve",
        "config",
        "envs",
        "migrate",
        "storage:prune_blocks",
        "test",
        "pack",
        "tui",
        "ggconf",
        "prune_containers",
        "refresh_clients",
        "tx_history",
        "increase_peer_deposit",
        "verify_reputation",
        "pay_and_verify",
        "local_docker_packer",
        "daemon",
        "doctor",
        "completion",
    }
)

# Repository root (…/nodo), derived from this file's location so the standalone
# invocation can import ``src`` / ``protos`` regardless of the caller's cwd.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --- Candidate sources -------------------------------------------------------


def config_paths() -> Dict[str, Optional[str]]:
    """Resolve registry / metadata / database paths via nodo's own config."""
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from src.utils.config import ConfigManager  # deferred: reads config.yaml

    # Load config.yaml from the nodo install dir, NOT the caller's cwd. The shell
    # runs this helper from wherever the user is standing, so a bare relative
    # "config.yaml" would resolve against that cwd, find nothing, and silently kill
    # service / instance / peer id completion everywhere but the nodo directory.
    nodo_dir = os.environ.get("NODO_COMPLETION_DIR", _ROOT)
    env = ConfigManager(os.path.join(nodo_dir, "config.yaml"))
    return {
        "registry": env.get("REGISTRY"),
        "metadata": env.get("METADATA_REGISTRY"),
        "database": env.get("DATABASE_FILE"),
    }


def _read_tag(metadata_path: str) -> Optional[str]:
    """Return a service's first tag from its protobuf metadata, if any."""
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    try:
        from protos import celaut_pb2  # deferred; does not import grpc

        metadata = celaut_pb2.Metadata()
        with open(metadata_path, "rb") as handle:
            metadata.ParseFromString(handle.read())
        if metadata.hashtag.tag:
            return metadata.hashtag.tag[0]
    except Exception:
        return None
    return None


def service_candidates(registry: Optional[str], metadata: Optional[str]) -> List[str]:
    """Service content ids plus their (de-duplicated) tags.

    Ids are unique, but a tag is frequently shared by many services, so the list
    is de-duplicated to avoid the same tag appearing dozens of times.
    """
    candidates: List[str] = []
    seen: Dict[str, None] = {}

    def add(value: Optional[str]) -> None:
        if value and value not in seen:
            seen[value] = None
            candidates.append(value)

    if not registry:
        return candidates
    try:
        ids = sorted(os.listdir(registry))
    except OSError:
        return candidates
    for service_id in ids:
        add(service_id)
        if metadata:
            tag = _read_tag(os.path.join(metadata, service_id))
            if tag and tag != service_id:
                add(tag)
    return candidates


def _sqlite_column(database: Optional[str], query: str) -> List[str]:
    """Run a read-only query and return the flattened, de-duplicated columns."""
    if not database or not os.path.exists(database):
        return []
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    seen: Dict[str, None] = {}
    try:
        for row in connection.execute(query):
            for value in row:
                if value:
                    seen.setdefault(str(value), None)
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return list(seen)


def instance_candidates(database: Optional[str]) -> List[str]:
    """Instance ids and their human-friendly names."""
    return _sqlite_column(database, "SELECT id, name FROM local_instances")


def peer_candidates(database: Optional[str]) -> List[str]:
    """Known peer ids."""
    return _sqlite_column(database, "SELECT id FROM peer")


def candidates(kind: str, paths: Optional[Dict[str, Optional[str]]] = None) -> List[str]:
    """Return completion candidates for a ``kind`` requested by the shell."""
    if kind == "commands":
        return COMMANDS
    if kind == "daemon":
        return DAEMON_SUBCOMMANDS
    if paths is None:
        try:
            paths = config_paths()
        except Exception:
            return []
    if kind == "services":
        return service_candidates(paths.get("registry"), paths.get("metadata"))
    if kind == "instances":
        return instance_candidates(paths.get("database"))
    if kind == "peers":
        return peer_candidates(paths.get("database"))
    if kind == "refs":
        return (
            service_candidates(paths.get("registry"), paths.get("metadata"))
            + instance_candidates(paths.get("database"))
            + peer_candidates(paths.get("database"))
        )
    return []


# --- Shell script generation -------------------------------------------------


def _quote_words(words: List[str]) -> str:
    return " ".join(words)


def bash_script(nodo_dir: str, python_bin: str) -> str:
    """Render the bash completion script with paths baked in."""
    return f"""# nodo bash completion (generated by `nodo completion bash`)
_nodo_completion() {{
    local cur cword
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    cword=$COMP_CWORD

    local nodo_py="{python_bin}"
    local nodo_dir="{nodo_dir}"
    # Pass the install dir so the helper loads nodo's config.yaml regardless of the
    # user's current directory.
    _nodo_cand() {{ NODO_COMPLETION_DIR="$nodo_dir" "$nodo_py" "$nodo_dir/src/commands/completion.py" list "$1" 2>/dev/null; }}
    # Path completion that actually descends directories (trailing slash) and keeps
    # filenames intact — `compopt -o filenames` is what plain `compgen -f` lacks.
    _nodo_paths() {{
        compopt -o filenames 2>/dev/null
        COMPREPLY=( $(compgen -f -- "$cur") )
    }}

    # Complete the command itself (static, space-separated list -> default IFS).
    if [ "$cword" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "{_quote_words(COMMANDS)}" -- "$cur") )
        return 0
    fi

    local cmd="${{COMP_WORDS[1]}}"
    # First positional argument: an id/tag, a path, or a daemon subcommand.
    if [ "$cword" -eq 2 ]; then
        local kind=""
        case "$cmd" in
            {"|".join(SERVICE_COMMANDS)}) kind="services" ;;
            {"|".join(INSTANCE_COMMANDS)}) kind="instances" ;;
            {"|".join(PEER_COMMANDS)}) kind="peers" ;;
            {"|".join(PATH_COMMANDS)}) _nodo_paths; return 0 ;;
            daemon)
                COMPREPLY=( $(compgen -W "{_quote_words(DAEMON_SUBCOMMANDS)}" -- "$cur") )
                return 0
                ;;
        esac
        if [ -n "$kind" ]; then
            # Candidates come newline-separated; split on newlines only so tags
            # that contain spaces survive as single completions.
            local IFS=$'\\n'
            COMPREPLY=( $(compgen -W "$(_nodo_cand "$kind")" -- "$cur") )
            return 0
        fi
        # Unknown command's first argument -> offer filesystem paths.
        _nodo_paths
        return 0
    fi

    # Second positional argument that is a path (e.g. `export <service> <path>`).
    if [ "$cword" -eq 3 ]; then
        case "$cmd" in
            {"|".join(SECOND_ARG_PATH_COMMANDS)}) _nodo_paths; return 0 ;;
        esac
    fi

    # Everything else falls back to filename completion.
    _nodo_paths
    return 0
}}
complete -o bashdefault -o default -F _nodo_completion nodo
"""


def zsh_script(nodo_dir: str, python_bin: str) -> str:
    """Render the zsh completion script with paths baked in."""
    return f"""#compdef nodo
# nodo zsh completion (generated by `nodo completion zsh`)
_nodo() {{
    local nodo_py="{python_bin}"
    local nodo_dir="{nodo_dir}"
    local -a items
    # Pass the install dir so the helper loads nodo's config.yaml regardless of cwd.
    _nodo_cand() {{ NODO_COMPLETION_DIR="$nodo_dir" "$nodo_py" "$nodo_dir/src/commands/completion.py" list "$1" 2>/dev/null }}

    if (( CURRENT == 2 )); then
        items=({_quote_words(COMMANDS)})
        _describe -t commands 'nodo command' items
        return
    fi

    local cmd=${{words[2]}}
    if (( CURRENT == 3 )); then
        local kind=""
        case $cmd in
            {"|".join(SERVICE_COMMANDS)}) kind="services" ;;
            {"|".join(INSTANCE_COMMANDS)}) kind="instances" ;;
            {"|".join(PEER_COMMANDS)}) kind="peers" ;;
            {"|".join(PATH_COMMANDS)}) _files; return ;;
            daemon)
                items=({_quote_words(DAEMON_SUBCOMMANDS)})
                _describe -t subcommands 'daemon subcommand' items
                return
                ;;
        esac
        if [[ -n $kind ]]; then
            items=(${{(f)"$(_nodo_cand $kind)"}})
            _describe -t $kind $kind items
            return
        fi
    fi

    # Second positional path argument (e.g. `export <service> <path>`).
    if (( CURRENT == 4 )); then
        case $cmd in
            {"|".join(SECOND_ARG_PATH_COMMANDS)}) _files; return ;;
        esac
    fi

    _files
}}
_nodo "$@"
"""


def _completion_targets(user: bool) -> Dict[str, str]:
    """Filesystem destinations for the generated scripts."""
    if user:
        home = os.path.expanduser("~")
        return {
            "bash": os.path.join(home, ".local/share/bash-completion/completions/nodo"),
            "zsh": os.path.join(home, ".zsh/completions/_nodo"),
        }
    return {
        "bash": "/etc/bash_completion.d/nodo",
        "zsh": "/usr/local/share/zsh/site-functions/_nodo",
    }


def install(nodo_dir: str, python_bin: str, user: Optional[bool] = None) -> List[str]:
    """Write the bash and zsh scripts, returning the paths written.

    ``user=None`` picks system-wide when running as root, else the per-user
    directories. Never edits the user's shell rc files.
    """
    if user is None:
        user = os.geteuid() != 0 if hasattr(os, "geteuid") else True
    targets = _completion_targets(user)
    rendered = {
        "bash": bash_script(nodo_dir, python_bin),
        "zsh": zsh_script(nodo_dir, python_bin),
    }
    written: List[str] = []
    for shell, path in targets.items():
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(rendered[shell])
            written.append(path)
        except OSError as error:
            print(f"Could not write {shell} completion to {path}: {error}", file=sys.stderr)
    return written


# --- CLI ---------------------------------------------------------------------


def main(argv: List[str]) -> int:
    """Entry point for ``python completion.py …`` and ``nodo completion …``."""
    nodo_dir = os.environ.get("NODO_COMPLETION_DIR", _ROOT)
    python_bin = os.environ.get("NODO_COMPLETION_PY", sys.executable)

    action = argv[0] if argv else "help"

    if action == "list":
        kind = argv[1] if len(argv) > 1 else "commands"
        for candidate in candidates(kind):
            print(candidate)
        return 0

    if action == "bash":
        print(bash_script(nodo_dir, python_bin))
        return 0

    if action == "zsh":
        print(zsh_script(nodo_dir, python_bin))
        return 0

    if action == "install":
        user: Optional[bool] = None
        if "--user" in argv:
            user = True
        elif "--system" in argv:
            user = False
        written = install(nodo_dir, python_bin, user=user)
        if not written:
            print("No completion scripts were installed.", flush=True)
            return 1
        print("Installed shell completion:", flush=True)
        for path in written:
            print(f"  {path}", flush=True)
        print(
            "\nOpen a new shell (or `source` the file above) to activate it. "
            "For bash you need the `bash-completion` package; for zsh ensure the "
            "install directory is on your `fpath`.",
            flush=True,
        )
        return 0

    print(
        "Usage: nodo completion <bash|zsh|install [--user|--system]>\n"
        "       nodo completion list <commands|services|instances|peers|refs>",
        flush=True,
    )
    return 0 if action == "help" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
