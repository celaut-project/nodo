"""Which ``nodo tunnel`` processes are running on this host, and how to stop them.

A tunnel is a process, not a row: ``nodo tunnel`` binds a local listener and relays
until it is stopped. Nothing recorded it, so the only way to find one again was to
remember the terminal it ran in -- and an agent, or the TUI, had no terminal to
remember. Each tunnel process now leaves a small JSON file here for as long as it
runs, and removes it on the way out:

    <main.STORAGE>/tunnels/<id>.json    what it is (see :func:`new_record`)
    <main.STORAGE>/tunnels/<id>.log     its output, for one started with --detach

This is not the server-side registry ``docs/TUNNELING.md`` says was dropped. These
are the *client* ends this host opened, which is what an operator can list and close.
The other end -- the ``ServiceTunnel`` streams this node relays for others -- is kept
in the daemon's memory (``src/tunneling/inbound.py``) and mirrored to
``inbound.snapshot`` here, which :func:`read_inbound` reads.

A file whose process is gone -- killed with -9, or a host that rebooted -- is not a
tunnel, so readers check the pid and sweep the file instead of reporting it. The
TUI reads the same files (``src/commands/tui/src/tunnels.rs``); the field names are
the contract between the two.

Stdlib only, and the storage directory is resolved lazily: ``nodo tunnels`` and the
tests must not need the gateway import graph to read a directory.
"""

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Overrides the directory, for tests and for a node whose storage is elsewhere.
DIR_ENV = "NODO_TUNNELS_DIR"
#: Set by ``--detach`` on the process it starts, so that process registers under the
#: id its parent is waiting for.
ID_ENV = "NODO_TUNNEL_ID"

RECORD_SUFFIX = ".json"
LOG_SUFFIX = ".log"
#: What a detached tunnel was opened with, so the daemon can open it again after a
#: restart (:func:`restore`). Kept until the tunnel is closed on purpose.
SPEC_SUFFIX = ".spec"
#: Daemon starts in a row a persisted tunnel may fail to come back before it is
#: dropped -- a port someone else took, say -- rather than retried forever.
RESTORE_ATTEMPTS = 3

#: The relaying side: the ``ServiceTunnel`` streams the node carries for others, as
#: the daemon last wrote them (``src/tunneling/inbound.py``). Not ``.json``, so no
#: reader of the client records above mistakes it for one.
INBOUND_FILE = "inbound.snapshot"
#: The daemon rewrites the snapshot every few seconds while a stream is open; one
#: that lists streams and is older than this was left by a daemon that is gone.
INBOUND_STALE_S = 30


def registry_dir() -> str:
    override = os.environ.get(DIR_ENV)
    if override:
        return override
    from src.utils.config import ConfigManager

    storage = ConfigManager().get("main.STORAGE") or "storage"
    return os.path.join(str(storage), "tunnels")


def new_id() -> str:
    return secrets.token_hex(4)


def record_path(tunnel_id: str, directory: Optional[str] = None) -> str:
    return os.path.join(directory or registry_dir(), tunnel_id + RECORD_SUFFIX)


def log_path(tunnel_id: str, directory: Optional[str] = None) -> str:
    return os.path.join(directory or registry_dir(), tunnel_id + LOG_SUFFIX)


def spec_path(tunnel_id: str, directory: Optional[str] = None) -> str:
    return os.path.join(directory or registry_dir(), tunnel_id + SPEC_SUFFIX)


def new_record(
    tunnel_id: str,
    instance: str,
    token: str,
    slot: int,
    udp: bool,
    listen_host: str,
    listen_port: int,
    gateway: str,
    peer: Optional[str],
    detached: bool,
    pid: Optional[int] = None,
    log: Optional[str] = None,
    open_fee_mu: Optional[int] = None,
) -> Dict[str, Any]:
    """The one shape a tunnel is described in, by the CLI and the TUI alike.

    ``open_fee_mu`` is what each connection through it spends of the instance's
    balance to open (``pricing.TUNNEL_OPEN_MU``; traffic is billed on top). None
    through ``--peer``: the remote node charges its own price, which is not known here.
    """
    return {
        "id": tunnel_id,
        "pid": pid if pid is not None else os.getpid(),
        "instance": instance,
        "token": token,
        "slot": slot,
        "transport": "udp" if udp else "tcp",
        "listen_host": listen_host,
        "listen_port": listen_port,
        "gateway": gateway,
        "peer": peer,
        "detached": detached,
        "log": log,
        "started_at": int(time.time()),
        "open_fee_mu": open_fee_mu,
    }


def register(record: Dict[str, Any], directory: Optional[str] = None) -> str:
    """Write ``record`` atomically, so a reader never sees half of it."""
    directory = directory or registry_dir()
    os.makedirs(directory, exist_ok=True)
    path = record_path(record["id"], directory)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w") as handle:
        json.dump(record, handle)
    os.replace(temporary, path)
    return path


def unregister(tunnel_id: str, directory: Optional[str] = None, keep_log: bool = False) -> None:
    paths = [record_path(tunnel_id, directory)]
    if not keep_log:
        paths.append(log_path(tunnel_id, directory))
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def pid_alive(pid: Any) -> bool:
    """Whether ``pid`` is a running ``nodo tunnel``.

    ``kill(pid, 0)`` answers "is there a process"; on Linux its command line also says
    whether it is still *ours*, so a pid recycled after a reboot does not keep a dead
    tunnel listed.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # Someone else's process (root's tunnel, read by a user): it exists.
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = handle.read().split(b"\0")
    except OSError:
        return True  # No /proc to ask: existence is all there is to go on.
    return b"tunnel" in argv


def _read(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("id") else None


def list_tunnels(directory: Optional[str] = None, sweep: bool = True) -> List[Dict[str, Any]]:
    """Every tunnel whose process is running, oldest first.

    ``sweep`` removes the files of the ones that are not -- best effort, since a user
    reading a root-owned directory can list it but not clean it.
    """
    directory = directory or registry_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    tunnels = []
    now = time.time()
    for name in names:
        if not name.endswith(RECORD_SUFFIX):
            continue
        record = _read(os.path.join(directory, name))
        if record is None:
            continue
        if not pid_alive(record.get("pid")):
            if sweep:
                try:
                    unregister(record["id"], directory)
                except OSError:
                    pass
            continue
        started = record.get("started_at")
        record["age_secs"] = int(now - started) if isinstance(started, (int, float)) else None
        # Reopened by the daemon after a restart (see `restore`).
        record["persistent"] = os.path.exists(spec_path(record["id"], directory))
        tunnels.append(record)
    tunnels.sort(key=lambda record: (record.get("started_at") or 0, record["id"]))
    return tunnels


def find(reference: str, directory: Optional[str] = None) -> List[Dict[str, Any]]:
    """Running tunnels whose id is, or starts with, ``reference``.

    An exact id wins outright; otherwise every prefix match is returned, so the
    caller can tell "no such tunnel" from "say more".
    """
    tunnels = list_tunnels(directory)
    exact = [record for record in tunnels if record["id"] == reference]
    if exact:
        return exact
    return [record for record in tunnels if reference and record["id"].startswith(reference)]


def close(record: Dict[str, Any], directory: Optional[str] = None, grace_s: float = 5.0) -> bool:
    """Stop one tunnel: SIGTERM, then SIGKILL if it outlives ``grace_s``.

    The process removes its own file when it exits on SIGTERM; the file is removed
    here as well, for one that had to be killed. False only when the process could
    not be signalled (another user's, typically).
    """
    pid = int(record["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        unregister(record["id"], directory)
        forget(record["id"], directory)
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    unregister(record["id"], directory)
    # Closed on purpose (tunnel_close, the TUI's `d`, kill): not to be reopened.
    forget(record["id"], directory)
    return True


def reaches(record: Dict[str, Any], references) -> bool:
    """Whether ``record`` is a tunnel through this node to an instance ``references``
    names (its id, its name, or whatever the operator typed when opening it).

    A ``--peer`` tunnel reaches an instance of another node, which shares nothing
    with the instances here but the shape of its token.
    """
    if record.get("peer"):
        return False
    references = {reference for reference in references if reference}
    return record.get("token") in references or record.get("instance") in references


def for_instance(references, directory: Optional[str] = None) -> List[Dict[str, Any]]:
    """The running tunnels that reach the instance ``references`` names."""
    return [record for record in list_tunnels(directory) if reaches(record, references)]


def log_tail(record: Dict[str, Any], lines: int = 20) -> List[str]:
    path = record.get("log")
    if not path:
        return []
    try:
        with open(path, errors="replace") as handle:
            return [line.rstrip("\n") for line in handle.readlines()[-lines:]]
    except OSError:
        return []


def inbound_path(directory: Optional[str] = None) -> str:
    return os.path.join(directory or registry_dir(), INBOUND_FILE)


def write_inbound(streams: List[Dict[str, Any]], directory: Optional[str] = None) -> None:
    """Replace the inbound snapshot (the daemon's side), atomically."""
    directory = directory or registry_dir()
    os.makedirs(directory, exist_ok=True)
    path = inbound_path(directory)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w") as handle:
        json.dump({"pid": os.getpid(), "written_at": int(time.time()), "streams": streams}, handle)
    os.replace(temporary, path)


def process_exists(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # The daemon runs as root; a user asking can see it exists, no more.
    return True


def read_inbound(directory: Optional[str] = None) -> Dict[str, Any]:
    """The streams the node is relaying for others, as ``{"streams", "snapshot_at"}``.

    Empty, with ``snapshot_at`` None, when there is no snapshot or it was left by a
    daemon that is no longer running: those streams died with it.
    """
    try:
        with open(inbound_path(directory)) as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {"streams": [], "snapshot_at": None}
    if not isinstance(document, dict) or not process_exists(document.get("pid")):
        return {"streams": [], "snapshot_at": None}
    written_at = document.get("written_at")
    streams = [stream for stream in document.get("streams") or [] if isinstance(stream, dict)]
    now = time.time()
    if streams and (not isinstance(written_at, (int, float)) or now - written_at > INBOUND_STALE_S):
        return {"streams": [], "snapshot_at": None}
    for stream in streams:
        started = stream.get("started_at")
        stream["age_secs"] = int(now - started) if isinstance(started, (int, float)) else None
    streams.sort(key=lambda stream: (stream.get("started_at") or 0, str(stream.get("id"))))
    return {"streams": streams, "snapshot_at": written_at}


# -- Detached tunnels, and bringing them back after a restart ------------------------


def spawn_detached(
    argv: List[str],
    nodo_py: str,
    tunnel_id: Optional[str] = None,
    timeout_s: float = 60.0,
    directory: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run ``nodo tunnel <argv>`` in the background; ``(record, None)`` once it is up.

    The child is the same command with its output sent to ``<id>.log`` beside its
    record, in a session of its own so closing a terminal does not take it down. It
    reports success the only way that cannot lie: by registering, which it does after
    binding. If it exits first, its last log line is the error.
    """
    tunnel_id = tunnel_id or new_id()
    directory = directory or registry_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        log_file = open(log_path(tunnel_id, directory), "ab")
    except OSError as e:
        return None, f"Error: cannot write the tunnel registry {directory} ({e})."

    with log_file:
        child = subprocess.Popen(
            [sys.executable, nodo_py, "tunnel", *argv],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, **{DIR_ENV: directory, ID_ENV: tunnel_id}),
            start_new_session=True,
        )

    path = record_path(tunnel_id, directory)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.exists(path):
            break
        if child.poll() is not None:
            # Its last words, not the whole log: start-up chatter comes first.
            said = [line.strip() for line in log_tail({"log": log_file.name}) if line.strip()]
            unregister(tunnel_id, directory)
            return None, said[-1] if said else f"Error: the tunnel exited ({child.returncode})."
        time.sleep(0.1)
    else:
        child.kill()
        unregister(tunnel_id, directory)
        return None, f"Error: the tunnel did not start within {int(timeout_s)}s."

    found = find(tunnel_id, directory)
    if not found:
        return None, "Error: the tunnel registered and exited at once."
    return found[0], None


def save_spec(record: Dict[str, Any], argv: List[str], directory: Optional[str] = None,
              failures: int = 0) -> None:
    """Remember how to reopen a detached tunnel.

    ``argv`` is pinned to the port it got: a client given that address after a
    restart cannot be told about another one.
    """
    argv = list(argv)
    if "--listen" not in argv and record.get("listen_port"):
        argv += ["--listen", str(record["listen_port"])]
    spec = {
        "id": record["id"],
        "argv": argv,
        "instance": record.get("instance"),
        "token": record.get("token"),
        "peer": record.get("peer"),
        "slot": record.get("slot"),
        "transport": record.get("transport"),
        "listen_port": record.get("listen_port"),
        "saved_at": int(time.time()),
        "failures": failures,
    }
    directory = directory or registry_dir()
    os.makedirs(directory, exist_ok=True)
    path = spec_path(record["id"], directory)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w") as handle:
        json.dump(spec, handle)
    os.replace(temporary, path)


def forget(tunnel_id: str, directory: Optional[str] = None) -> None:
    try:
        os.remove(spec_path(tunnel_id, directory))
    except FileNotFoundError:
        pass


def list_specs(directory: Optional[str] = None) -> List[Dict[str, Any]]:
    directory = directory or registry_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    specs = []
    for name in names:
        if not name.endswith(SPEC_SUFFIX):
            continue
        spec = _read(os.path.join(directory, name))
        if spec is not None and isinstance(spec.get("argv"), list):
            specs.append(spec)
    return specs


def restore(
    nodo_py: str,
    instance_exists: Callable[[str], bool],
    log: Callable[[str], None] = print,
    directory: Optional[str] = None,
    timeout_s: float = 60.0,
) -> int:
    """Reopen the detached tunnels that were running before a restart.

    Called once when the daemon starts, the way delegated endpoints are restored.
    Each comes back under its own id, on its own port, through the same detach path
    ``nodo tunnel --detach`` takes. One still running (a daemon restart that did not
    take it down) is left alone. One whose instance is gone is dropped with a log
    line; one that fails to start is retried on the next start, up to
    ``RESTORE_ATTEMPTS`` in a row, then dropped. Returns how many were reopened.
    """
    directory = directory or registry_dir()
    restored = 0
    for spec in list_specs(directory):
        tunnel_id = spec["id"]
        if any(record["id"] == tunnel_id for record in list_tunnels(directory)):
            continue
        target = spec.get("token") or spec.get("instance") or ""
        if not spec.get("peer") and not instance_exists(target):
            forget(tunnel_id, directory)
            log(f"[TUNNEL] Dropped tunnel {tunnel_id}: instance {target} no longer exists.")
            continue
        record, error = spawn_detached(
            spec["argv"], nodo_py, tunnel_id=tunnel_id, timeout_s=timeout_s, directory=directory,
        )
        if record is not None:
            save_spec(record, spec["argv"], directory)
            restored += 1
            log(f"[TUNNEL] Reopened tunnel {tunnel_id} on "
                f"{record['listen_host']}:{record['listen_port']} -> slot {record['slot']}.")
            continue
        failures = int(spec.get("failures") or 0) + 1
        if failures >= RESTORE_ATTEMPTS:
            forget(tunnel_id, directory)
            log(f"[TUNNEL] Dropped tunnel {tunnel_id} after {failures} failed reopenings: {error}")
        else:
            spec["failures"] = failures
            path = spec_path(tunnel_id, directory)
            with open(path, "w") as handle:
                json.dump(spec, handle)
            log(f"[TUNNEL] Could not reopen tunnel {tunnel_id} ({error}); "
                f"will retry on the next start.")
    return restored
