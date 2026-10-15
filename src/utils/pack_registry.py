"""Which ``nodo pack`` runs this host has started, how far each got, and how to stop one.

A pack is a process that runs for minutes -- a clone, a Docker build inside a packer
VM or nodo's rootless builder, an import -- and until now it was only visible in the
terminal that started it. Every ``nodo pack`` now leaves a JSON file here, keeps it
current while it runs, and leaves it behind with the outcome when it ends:

    <main.STORAGE>/packs/<id>.json    what it packs and where it is (see :func:`new_record`)
    <main.STORAGE>/packs/<id>.log     its output, for one started with --detach

Unlike a tunnel's file, a pack's is kept after the process exits: the result (the
service id, or why there is none) is the point of looking. The newest
``KEEP_FINISHED`` finished ones are kept and older ones pruned.

A record that says ``queued``/``running`` but whose process is gone -- killed with
-9, or a host that rebooted -- did not finish; readers mark it ``failed`` instead of
reporting it as running. The TUI reads the same files
(``src/commands/tui/src/packs.rs``); the field names are the contract between the two.

Stdlib only, and the storage directory is resolved lazily, like
``tunnel_registry``: ``nodo packs`` and the tests must not need the packer's
import graph to read a directory.
"""

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

#: Overrides the directory, for tests and for a node whose storage is elsewhere.
DIR_ENV = "NODO_PACKS_DIR"
#: Set by ``--detach`` on the process it starts, so that process registers under the
#: id its parent is waiting for.
ID_ENV = "NODO_PACK_ID"

RECORD_SUFFIX = ".json"
LOG_SUFFIX = ".log"

#: Statuses. ``queued``: waiting for another local pack to release nodo's builder.
QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
ACTIVE = (QUEUED, RUNNING)
#: Finished packs kept for ``nodo packs`` / the TUI; older ones are pruned.
KEEP_FINISHED = 20
#: What a pack whose process vanished without reporting is marked with.
LOST_ERROR = "the pack process exited without reporting (killed, or the host restarted)"


class PackCancelled(BaseException):
    """Raised in a pack process by SIGTERM (``nodo pack_cancel``).

    A ``BaseException`` on purpose: the packers catch ``Exception`` to print and
    return None, and a cancel must not be reported as an ordinary packing error --
    it has to unwind through their ``finally`` blocks (stop the builder, remove the
    clone, release the pack lock) and out to :func:`run`.
    """


# -- Paths and records ----------------------------------------------------------------


def registry_dir() -> str:
    override = os.environ.get(DIR_ENV)
    if override:
        return override
    from src.utils.config import ConfigManager

    storage = ConfigManager().get("main.STORAGE") or "storage"
    return os.path.join(str(storage), "packs")


def new_id() -> str:
    return secrets.token_hex(4)


def record_path(pack_id: str, directory: Optional[str] = None) -> str:
    return os.path.join(directory or registry_dir(), pack_id + RECORD_SUFFIX)


def log_path(pack_id: str, directory: Optional[str] = None) -> str:
    return os.path.join(directory or registry_dir(), pack_id + LOG_SUFFIX)


def new_record(
    pack_id: str,
    source: str,
    kind: str,
    packer: str,
    detached: bool,
    pid: Optional[int] = None,
    log: Optional[str] = None,
) -> Dict[str, Any]:
    """The one shape a pack is described in, by the CLI and the TUI alike."""
    return {
        "id": pack_id,
        "pid": pid if pid is not None else os.getpid(),
        "source": source,
        "kind": kind,  # "git" or "dir"
        "packer": packer,  # "local" (packer.local) or "service" (packer-service)
        "status": RUNNING,
        "stage": "starting",
        "detached": detached,
        "log": log,
        "started_at": int(time.time()),
        "finished_at": None,
        "service_id": None,
        "error": None,
    }


def _name_of(uid: int) -> str:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def unwritable_hint(directory: str) -> str:
    """Who owns ``directory`` and what to run, when this process cannot write to it.

    The usual cause is a registry created by a root run (``sudo nodo pack``, the
    daemon, the TUI): a later pack as a normal user finds the directory, cannot
    create a file in it, and until now said only ``Permission denied`` about a
    temporary file's name (#476). Empty when the directory is not owned by someone
    else, because then ``chown`` would not help -- a full or read-only disk says so in
    the error itself.
    """
    existing = os.path.abspath(directory)
    while existing and not os.path.exists(existing):
        parent = os.path.dirname(existing)
        if parent == existing:
            return ""
        existing = parent
    try:
        owner = os.stat(existing).st_uid
    except OSError:
        return ""
    me = os.geteuid() if hasattr(os, "geteuid") else owner
    if owner == me:
        return ""
    where = "" if existing == os.path.abspath(directory) else f" (its nearest existing parent {existing})"
    return (
        f"{directory} belongs to {_name_of(owner)}{where}, and this pack runs as "
        f"{_name_of(me)}. Give it to the user who packs: "
        f"sudo chown -R {_name_of(me)} {existing}"
    )


def _unwritable_message(directory: str, error: OSError) -> str:
    hint = unwritable_hint(directory)
    return f"{error}. {hint}" if hint else str(error)


def write(record: Dict[str, Any], directory: Optional[str] = None) -> str:
    """Write ``record`` atomically, so a reader never sees half of it."""
    directory = directory or registry_dir()
    os.makedirs(directory, exist_ok=True)
    path = record_path(record["id"], directory)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w") as handle:
        json.dump(record, handle)
    os.replace(temporary, path)
    return path


def remove(pack_id: str, directory: Optional[str] = None) -> None:
    for path in (record_path(pack_id, directory), log_path(pack_id, directory)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _read(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("id") else None


def pid_alive(pid: Any) -> bool:
    """Whether ``pid`` is a running ``nodo pack`` (``tunnel_registry.pid_alive``'s twin).

    Where ``/proc`` exists its command line must still say ``pack``, so a pid recycled
    after a reboot does not keep a dead pack "running".
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        # One this process started (``spawn_detached`` in a long-lived caller) and that
        # has exited is a zombie until reaped, and a zombie still answers kill(0).
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass  # Not our child: the usual case.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # Someone else's process (root's pack, read by a user): it exists.
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = handle.read().split(b"\0")
    except OSError:
        return True
    return b"pack" in argv


# -- Validating what to pack --------------------------------------------------------------


def validate_source(text: str, base_dir: Optional[str] = None) -> Tuple[str, str]:
    """``(kind, source)`` for what ``nodo pack`` was given, or ``ValueError`` saying why not.

    * ``https://host/owner/repo[.git][#subdir]`` -> ``("git", text)``. The packer
      clones it without credentials, so only https: ``http://`` can be rewritten in
      transit (and a pack seals whatever arrived), and ``ssh://`` / ``git@`` need a
      key this process does not have.
    * anything else is a local directory, relative to ``base_dir`` (the shell the
      operator typed in, ``ORIGINAL_DIR``) -> ``("dir", <absolute path>)``. It must
      exist and be a directory.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Give a project directory or an https git URL to pack.")
    lowered = text.lower()
    if lowered.startswith("https://"):
        url, _, subdir = text.partition("#")
        parts = urlsplit(url)
        if not parts.netloc or parts.path.strip("/") == "":
            raise ValueError(f"'{text}' is not a repository URL (https://host/owner/repo.git).")
        if any(c.isspace() for c in text):
            raise ValueError("A git URL cannot contain spaces.")
        if subdir and (subdir.startswith("/") or ".." in subdir.split("/")):
            raise ValueError(f"'#{subdir}' must be a subdirectory inside the repository.")
        return "git", text
    if lowered.startswith("http://"):
        raise ValueError(
            "Plain http:// is refused: the code would be packed as it arrived, and over "
            "http it can be changed on the way. Use the https:// URL."
        )
    if lowered.startswith(("ssh://", "git://", "git@", "file://")) or (
        ":" in text.split("/")[0] and "@" in text.split("/")[0]
    ):
        raise ValueError(
            "Only https git URLs can be packed: the repository is cloned without "
            "credentials. Clone it yourself and pack the folder instead."
        )
    path = os.path.expanduser(text)
    if not os.path.isabs(path):
        path = os.path.join(base_dir or os.getcwd(), path)
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ValueError(f"The directory {path} does not exist.")
    if not os.path.isdir(path):
        raise ValueError(f"{path} is a file; give the project's directory.")
    return "dir", path


# -- The running pack's own record ----------------------------------------------------------

#: The record of the pack running in *this* process, and where it lives. Set by
#: :func:`run`; :func:`stage` and :func:`note_error` are no-ops without it, so the
#: packers can call them unconditionally (nested dependency packs included).
_current: Dict[str, Any] = {}


def _update(**fields) -> None:
    record = _current.get("record")
    if record is None:
        return
    record.update(fields)
    try:
        write(record, _current.get("directory"))
    except OSError:
        pass  # Progress is best effort; the pack itself must not fail on it.


def stage(name: str) -> None:
    """Say where the pack is (``cloning``, ``building``, ...)."""
    _update(stage=name)


def queued(waiting: bool) -> None:
    """Waiting for another local pack to release the builder, or no longer."""
    _update(status=QUEUED if waiting else RUNNING,
            stage="waiting for another pack" if waiting else "starting")


def use_packer(name: str) -> None:
    """The pack changed to another packer (``local``). An error from the previous
    packer is not the reason this one fails, so it is removed."""
    _update(packer=name, error=None)


def note_error(message: str) -> None:
    """Why the pack failed. The first reason wins: a dependency that failed to pack is
    the cause, not the "packing produced no service id" that follows it."""
    record = _current.get("record")
    if record is None or record.get("error"):
        return
    message = " ".join(str(message).split())
    _update(error=message[:500])


def _raise_cancelled(signum, frame):
    raise PackCancelled()


def run(
    source: str,
    kind: str,
    packer: str,
    pack_fn,
    directory: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """Run ``pack_fn()`` as a registered pack; ``(service id or None, pack id)``.

    Detached (``ID_ENV`` set by :func:`spawn_detached`) it registers under the id the
    parent waits for, with its log beside it. SIGTERM -- ``nodo pack_cancel`` -- raises
    :class:`PackCancelled` so the packers clean up on the way out.
    """
    detached_id = os.environ.get(ID_ENV)
    pack_id = detached_id or new_id()
    directory = directory or registry_dir()
    record = new_record(
        pack_id, source, kind, packer,
        detached=bool(detached_id),
        log=log_path(pack_id, directory) if detached_id else None,
    )
    _current.update(record=record, directory=directory)
    try:
        write(record, directory)
    except OSError as e:
        # Packing does not need the registry; only listing it does.
        print(
            f"Warning: this pack is not listed by `nodo packs` ({_unwritable_message(directory, e)}).",
            flush=True,
        )
        _current.clear()

    previous = signal.signal(signal.SIGTERM, _raise_cancelled)
    service_id: Optional[str] = None
    outcome: Dict[str, Any] = {}
    try:
        service_id = pack_fn()
        if service_id:
            outcome = {"status": DONE, "service_id": service_id, "error": None}
        else:
            outcome = {"status": FAILED,
                       "error": record.get("error") or "packing produced no service id (see the log)"}
    except PackCancelled:
        print("\nPack cancelled.", flush=True)
        outcome = {"status": CANCELLED, "error": record.get("error") or "cancelled"}
    except BaseException as e:  # KeyboardInterrupt in a terminal, or a bug
        outcome = {"status": CANCELLED if isinstance(e, KeyboardInterrupt) else FAILED,
                   "error": record.get("error") or (str(e) or type(e).__name__)}
        if not isinstance(e, KeyboardInterrupt):
            raise
    finally:
        signal.signal(signal.SIGTERM, previous)
        if _current:
            _update(stage=None, finished_at=int(time.time()), **outcome)
        _current.clear()
    return service_id, pack_id


# -- Reading ----------------------------------------------------------------------------


def _clean_line(line: str) -> str:
    # A progress line redrawn with \r is one line in the file; its last state is what
    # was on screen.
    return line.rstrip("\n").split("\r")[-1].rstrip()


def log_tail(record: Dict[str, Any], lines: int = 20) -> List[str]:
    path = record.get("log")
    if not path:
        return []
    try:
        # newline="\n": a \r is a redraw within a line, not a line of its own.
        with open(path, errors="replace", newline="\n") as handle:
            text = handle.readlines()
    except OSError:
        return []
    return [_clean_line(line) for line in text[-lines:]]


def last_line(record: Dict[str, Any]) -> Optional[str]:
    said = [line for line in log_tail(record, 10) if line.strip()]
    return said[-1].strip() if said else None


def _settle(record: Dict[str, Any], directory: str, sweep: bool) -> Dict[str, Any]:
    """Mark a record whose process vanished mid-pack as failed (persisting it if allowed)."""
    if record.get("status") in ACTIVE and not pid_alive(record.get("pid")):
        record.update(status=FAILED, stage=None, error=record.get("error") or LOST_ERROR,
                      finished_at=record.get("finished_at") or int(time.time()))
        if sweep:
            try:
                write(record, directory)
            except OSError:
                pass
    return record


def list_packs(directory: Optional[str] = None, sweep: bool = True) -> List[Dict[str, Any]]:
    """Every pack on record, newest first, with ``age_secs``, ``duration_secs`` and the
    last line of its log (``last_line``).

    ``sweep`` writes back the ones found dead and prunes finished ones beyond
    ``KEEP_FINISHED`` -- best effort, since a user can list a root-owned directory
    without being able to change it.
    """
    directory = directory or registry_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    packs = []
    for name in names:
        if not name.endswith(RECORD_SUFFIX):
            continue
        record = _read(os.path.join(directory, name))
        if record is None:
            continue
        packs.append(_settle(record, directory, sweep))
    packs.sort(key=lambda r: (r.get("started_at") or 0, r["id"]), reverse=True)

    finished = [r for r in packs if r.get("status") not in ACTIVE]
    if sweep and len(finished) > KEEP_FINISHED:
        for old in finished[KEEP_FINISHED:]:
            try:
                remove(old["id"], directory)
            except OSError:
                continue
            packs.remove(old)

    now = time.time()
    for record in packs:
        started = record.get("started_at")
        finished_at = record.get("finished_at")
        record["age_secs"] = int(now - started) if isinstance(started, (int, float)) else None
        end = finished_at if isinstance(finished_at, (int, float)) else now
        record["duration_secs"] = (
            int(end - started) if isinstance(started, (int, float)) else None
        )
        record["last_line"] = last_line(record)
    return packs


def find(reference: str, directory: Optional[str] = None) -> List[Dict[str, Any]]:
    """Packs whose id is, or starts with, ``reference`` (an exact id wins outright)."""
    packs = list_packs(directory)
    exact = [record for record in packs if record["id"] == reference]
    if exact:
        return exact
    return [record for record in packs if reference and record["id"].startswith(reference)]


# -- Starting one in the background, and stopping it ---------------------------------------

#: How long ``--detach`` waits for the background pack to register. It registers
#: before any work, so this only covers the interpreter starting and nodo's imports.
DETACH_TIMEOUT_S = 60.0
#: How long a cancelled pack gets to unwind (stop the builder, remove its clone)
#: before its process group is killed.
CANCEL_GRACE_S = 30.0


def spawn_detached(
    source: str,
    nodo_py: str,
    pack_id: Optional[str] = None,
    timeout_s: float = DETACH_TIMEOUT_S,
    directory: Optional[str] = None,
    command: Optional[List[str]] = None,
    options: Optional[List[str]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run ``nodo pack <source>`` in the background; ``(record, None)`` once it registered.

    The child is the same command with its output in ``<id>.log`` beside its record,
    in a session of its own -- so closing the terminal (or quitting the TUI) does not
    stop it, and ``pack_cancel`` can kill its whole process group. If it exits before
    registering, its last log line is the error. ``command`` replaces the
    ``[python, nodo.py, "pack"]`` prefix (tests). ``options`` go after the source
    (for example ``--local``).
    """
    pack_id = pack_id or new_id()
    directory = directory or registry_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        log_file = open(log_path(pack_id, directory), "ab")
    except OSError as e:
        return None, f"Error: cannot write the pack registry {directory} ({_unwritable_message(directory, e)})."

    prefix = command or [sys.executable, nodo_py, "pack"]
    with log_file:
        child = subprocess.Popen(
            [*prefix, source, *(options or [])],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, **{DIR_ENV: directory, ID_ENV: pack_id, "PYTHONUNBUFFERED": "1"}),
            start_new_session=True,
        )

    path = record_path(pack_id, directory)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.exists(path):
            break
        if child.poll() is not None:
            said = [line.strip() for line in log_tail({"log": log_file.name}) if line.strip()]
            remove(pack_id, directory)
            return None, said[-1] if said else f"Error: the pack exited ({child.returncode})."
        time.sleep(0.1)
    else:
        child.kill()
        remove(pack_id, directory)
        return None, f"Error: the pack did not start within {int(timeout_s)}s."

    record = _read(path)
    if record is None:
        return None, "Error: the pack registered an unreadable record."
    return record, None


def cancel(
    record: Dict[str, Any],
    directory: Optional[str] = None,
    grace_s: float = CANCEL_GRACE_S,
) -> Tuple[bool, str]:
    """Stop a queued or running pack; ``(ok, message)``.

    SIGTERM first: the pack raises :class:`PackCancelled` and unwinds through the
    packers' cleanup (stops nodo's rootless builder, removes the clone or copy,
    releases the pack lock) and records itself ``cancelled``. One still alive after
    ``grace_s`` is killed -- with its whole process group when it was started
    detached (git, buildctl and the builder it started live there); a pack typed in a
    terminal shares that terminal's group, so only its own pid is killed.

    What it cannot stop: a build already sent to a packer *service* keeps running in
    that VM until it finishes; its result is simply never imported.
    """
    directory = directory or registry_dir()
    if record.get("status") not in ACTIVE:
        return False, f"Pack {record['id']} is not running ({record.get('status')})."
    pid = int(record["pid"])
    if not pid_alive(pid):
        _settle(record, directory, sweep=True)
        return False, f"Pack {record['id']} is not running (its process is gone)."
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, f"Pack {record['id']} had already exited."
    except PermissionError:
        return False, f"Pack {record['id']} belongs to another user (try sudo)."

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    # A detached pack's group is swept even after a clean exit: a `git clone` or
    # `buildctl` it was waiting on is not stopped by the exception that unwound it.
    if pid_alive(pid) or record.get("detached"):
        try:
            if record.get("detached"):
                os.killpg(pid, signal.SIGKILL)  # its own session: pgid == pid
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    current = _read(record_path(record["id"], directory)) or record
    if current.get("status") in ACTIVE:
        current.update(status=CANCELLED, stage=None, finished_at=int(time.time()),
                       error=current.get("error") or "cancelled (killed after the grace period)")
        try:
            write(current, directory)
        except OSError:
            pass
        return True, (f"Pack {record['id']} killed: it did not stop within {int(grace_s)}s. "
                      "If it was packing locally, nodo's builder may need "
                      "bash/stop_buildkit_daemon.sh.")
    return True, f"Pack {record['id']} cancelled."
