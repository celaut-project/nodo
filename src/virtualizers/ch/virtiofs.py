"""Cloud Hypervisor backend for shared filesystems (parent -> child inheritance).

The *semantics* of shared filesystems live in ``src/utils/shared_filesystems.py``
(the ``shared``/``guest``/``access`` xattrs, share identity, placement). This
module is the **backend**: it wires a virtio-fs mount for Cloud Hypervisor
microVMs so a child instance inherits a directory exported by the parent that
launched it.

For each share (identified by ``(parent_instance_id, export_path)``) we:

1. **virtiofsd daemon per share.** Export the share's host directory over a Unix
   socket, keyed by the share id. One daemon per share on this host, reused by
   the exporting parent and every child that inherits it.
2. **virtio-fs device per guest.** Emit a ``--fs tag=<…>,socket=<…>`` device so
   the guest can ``mount -t virtiofs <tag> <declared-path>``. A child that asked
   for ``access=ro`` mounts with ``-o ro``.
3. **Lifecycle & cleanup.** Tear the daemon + socket down when the last VM using
   the share on this host goes away (reference counted from the CH runtime
   state). The exported directory is the parent's data; it is removed only when
   the exporting parent itself is gone.
4. **Security.** Each daemon is confined to its own directory (``--sandbox
   chroot`` by default): deny-by-default, no cross-share access.

VirtioFS is purely an implementation detail here; the service spec never mentions
it. The pure builders at the top are unit-tested directly; this host cannot run
microVMs, so the spawn/teardown orchestration is dependency-injected and
exercised with fakes.
"""
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional

from protos import celaut_pb2 as celaut
from src.utils.shared_filesystems import (
    exported_dirs,
    guest_dirs,
    share_id,
)

# Guest metadata file injected into the rootfs: a JSON list of the virtiofs
# mounts the guest init should perform ({tag, path, ro}). Kept alongside the
# other guest-injected metadata.
GUEST_MOUNT_PLAN_PATH = "/.__nodo_virtiofs"


class SharedMount(NamedTuple):
    """One shared-filesystem mount a VM participates in."""
    share_id_hex: str    # identity of the share (sha256 of parent_id + path), hex
    tag: str             # virtio-fs tag (CH --fs tag= / guest mount tag)
    readonly: bool       # mount read-only in this guest
    guest_path: str      # where the directory is mounted inside the guest
    host_dir: str        # the exported host directory backing the share


# --------------------------------------------------------------------------- #
# Pure helpers (no side effects) — directly unit-tested.
# --------------------------------------------------------------------------- #

def shared_fs_base_dir(cache: str) -> Path:
    """Host directory that backs every share on this node.

    A single location so the launcher (which materializes shares) and the killer
    (which reference-counts and tears them down) always agree.
    """
    return Path(cache) / "cloud_hypervisor" / "shared_fs"


def virtiofs_tag(share_id_hex: str) -> str:
    """Stable, short virtio-fs tag derived from the share id."""
    return f"vfs-{share_id_hex[:16]}"


def share_state_dir(base_dir: str, share_id_hex: str) -> Path:
    """Per-share host directory: holds the exported data + daemon state."""
    return Path(base_dir) / share_id_hex


def shared_dir(base_dir: str, share_id_hex: str) -> Path:
    """The host directory exported to the guests of a share."""
    return share_state_dir(base_dir, share_id_hex) / "shared"


def daemon_state_path(base_dir: str, share_id_hex: str) -> Path:
    return share_state_dir(base_dir, share_id_hex) / "daemon.json"


def virtiofs_socket_path(socket_dir: str, share_id_hex: str) -> Path:
    """virtiofsd control socket, kept in the (short) CH API socket dir to stay
    under the AF_UNIX SUN_LEN limit."""
    return Path(socket_dir) / f"vfs-{share_id_hex[:16]}.sock"


def parent_export_mounts(
    service: celaut.Service,
    parent_instance_id: str,
    base_dir: str,
) -> List[SharedMount]:
    """Mounts for the directories this service *exports* to its children.

    The exporting parent mounts each shared directory read-write so writes land
    on the host directory the children will later inherit.
    """
    mounts: List[SharedMount] = []
    for d in exported_dirs(service):
        sid = share_id(parent_instance_id, d.path)
        mounts.append(SharedMount(
            share_id_hex=sid,
            tag=virtiofs_tag(sid),
            readonly=False,
            guest_path=d.path,
            host_dir=str(shared_dir(base_dir, sid)),
        ))
    return mounts


def child_guest_mounts(
    service: celaut.Service,
    father_id: str,
    base_dir: str,
) -> List[SharedMount]:
    """Mounts for the directories this service *inherits* from its parent.

    The share id is derived from the caller's own ``father_id`` and the guest
    path, so it necessarily matches the parent's export for that path — a child
    can never reference a directory some other instance exported.
    """
    if not father_id:
        return []
    mounts: List[SharedMount] = []
    for d in guest_dirs(service):
        sid = share_id(father_id, d.path)
        mounts.append(SharedMount(
            share_id_hex=sid,
            tag=virtiofs_tag(sid),
            readonly=d.readonly,
            guest_path=d.path,
            host_dir=str(shared_dir(base_dir, sid)),
        ))
    return mounts


def build_fs_device_arg(
    tag: str, socket_path: os.PathLike, *, num_queues: int = 1, queue_size: int = 1024
) -> str:
    """Value for cloud-hypervisor ``--fs`` (one virtio-fs device)."""
    return f"tag={tag},socket={socket_path},num_queues={num_queues},queue_size={queue_size}"


def build_virtiofsd_command(
    binary: str,
    socket_path: os.PathLike,
    export_dir: os.PathLike,
    *,
    sandbox: str = "chroot",
    cache: str = "auto",
) -> List[str]:
    """Command line for the rust ``virtiofsd`` daemon exporting ``export_dir``.

    The daemon is always read-write and confined to ``export_dir`` via
    ``--sandbox`` (deny-by-default). Read-only children mount the resulting
    device with ``-o ro`` on the guest side, so one daemon serves both the
    read-write parent and read-only children.
    """
    return [
        binary,
        "--socket-path", str(socket_path),
        "--shared-dir", str(export_dir),
        "--sandbox", sandbox,
        "--cache", cache,
    ]


def build_guest_mount_plan(mounts: List[SharedMount]) -> str:
    """JSON the guest init consumes to mount each shared filesystem."""
    return json.dumps(
        [
            {"tag": m.tag, "path": m.guest_path, "ro": m.readonly}
            for m in mounts
        ],
        sort_keys=True,
    )


def share_used_by_other_vm(
    share_id_hex: str,
    self_vmachine_id: str,
    runtime_states: Dict[str, dict],
) -> bool:
    """Reference count: does any OTHER live CH VM still use this share?"""
    for vmachine_id, state in runtime_states.items():
        if vmachine_id == self_vmachine_id:
            continue
        for mount in state.get("virtiofs") or []:
            if mount.get("share_id_hex") == share_id_hex:
                return True
    return False


# --------------------------------------------------------------------------- #
# Orchestration (side effects) — dependency-injected for testability.
# --------------------------------------------------------------------------- #

def _default_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _default_spawn(command: List[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(command, stdout=logf, stderr=logf)
    return proc.pid


def ensure_share_backend(
    mount: SharedMount,
    *,
    base_dir: str,
    socket_dir: str,
    virtiofsd_binary: str,
    sandbox: str = "chroot",
    spawn_fn: Callable[[List[str], Path], int] = _default_spawn,
    pid_alive_fn: Callable[[int], bool] = _default_pid_alive,
    logger_fn: Callable[[str], None] = lambda _m: None,
) -> Dict[str, object]:
    """Make sure a virtiofsd daemon is running for ``mount``'s share and return
    the state needed to attach a guest and, later, tear it down.

    Idempotent: if a daemon for this share is already alive with its socket
    present it is reused, so the exporting parent and every co-located child
    share a single daemon and a single host directory.
    """
    sid = mount.share_id_hex
    export_dir = shared_dir(base_dir, sid)
    socket_path = virtiofs_socket_path(socket_dir, sid)
    state_path = daemon_state_path(base_dir, sid)

    export_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(share_state_dir(base_dir, sid), 0o700)
    os.chmod(export_dir, 0o700)
    Path(socket_dir).mkdir(parents=True, exist_ok=True)

    # Reuse an existing healthy daemon if present.
    existing = _load_daemon_state(state_path)
    if existing and pid_alive_fn(int(existing.get("pid") or 0)) and socket_path.exists():
        logger_fn(f"[CH][virtiofs] share={sid} reusing daemon pid={existing.get('pid')}")
        return {
            "share_id_hex": sid,
            "tag": mount.tag,
            "socket": str(socket_path),
            "host_dir": str(export_dir),
            "pid": int(existing.get("pid") or 0),
            "readonly": mount.readonly,
            "guest_path": mount.guest_path,
        }

    # Stale socket from a dead daemon would block bind — clear it.
    try:
        socket_path.unlink(missing_ok=True)
    except OSError:
        pass

    command = build_virtiofsd_command(
        virtiofsd_binary, socket_path, export_dir, sandbox=sandbox
    )
    log_path = share_state_dir(base_dir, sid) / "virtiofsd.log"
    logger_fn(f"[CH][virtiofs] share={sid} starting daemon: {' '.join(command)}")
    pid = spawn_fn(command, log_path)

    _save_daemon_state(
        state_path,
        {
            "share_id_hex": sid,
            "pid": pid,
            "socket": str(socket_path),
            "host_dir": str(export_dir),
        },
    )
    return {
        "share_id_hex": sid,
        "tag": mount.tag,
        "socket": str(socket_path),
        "host_dir": str(export_dir),
        "pid": pid,
        "readonly": mount.readonly,
        "guest_path": mount.guest_path,
    }


def attach_virtiofs_backends(
    mounts: List[SharedMount],
    *,
    base_dir: str,
    socket_dir: str,
    virtiofsd_binary: str,
    sandbox: str = "chroot",
    spawn_fn: Callable[[List[str], Path], int] = _default_spawn,
    pid_alive_fn: Callable[[int], bool] = _default_pid_alive,
    logger_fn: Callable[[str], None] = lambda _m: None,
):
    """Ensure every shared filesystem in ``mounts`` has a running backend.

    Returns ``(fs_device_args, mounts_state, mounts)``:
      * ``fs_device_args``: flat argv to splice into the cloud-hypervisor command
        (``["--fs", "<arg>", …]``), one per share.
      * ``mounts_state``: JSON-serializable list persisted in the VM runtime
        state (drives reference-counted teardown).
      * ``mounts``: the :class:`SharedMount` list (for the guest mount plan).

    A VM with no shared filesystems yields empty lists — a complete no-op for
    ordinary services.
    """
    fs_device_args: List[str] = []
    mounts_state: List[Dict[str, object]] = []
    for mount in mounts:
        backend = ensure_share_backend(
            mount,
            base_dir=base_dir,
            socket_dir=socket_dir,
            virtiofsd_binary=virtiofsd_binary,
            sandbox=sandbox,
            spawn_fn=spawn_fn,
            pid_alive_fn=pid_alive_fn,
            logger_fn=logger_fn,
        )
        fs_device_args.extend(
            ["--fs", build_fs_device_arg(mount.tag, backend["socket"])]
        )
        mounts_state.append(backend)
    return fs_device_args, mounts_state, mounts


def teardown_virtiofs_for_vm(
    vmachine_id: str,
    mounts_state: List[dict],
    runtime_states: Dict[str, dict],
    *,
    base_dir: str,
    owned_share_ids=None,
    kill_fn: Callable[[int], None] = lambda pid: os.kill(pid, signal.SIGTERM),
    logger_fn: Callable[[str], None] = lambda _m: None,
) -> None:
    """Release the virtiofs backends a VM used; stop a daemon only when this was
    the last VM using its share on the host.

    ``owned_share_ids`` is the set of shares this VM *exported* (i.e. it is the
    parent). Only the exporting parent's departure removes the exported host
    directory; a departing child never deletes the parent's data. When
    ``owned_share_ids`` is None, every share torn down by this VM is treated as
    owned (used when the VM is the sole participant).

    ``runtime_states`` must be the CH runtime states with ``vmachine_id`` ALREADY
    removed, so the reference count does not see the VM being torn down.
    """
    owned = None if owned_share_ids is None else set(owned_share_ids)
    for mount in mounts_state or []:
        sid = mount.get("share_id_hex")
        if not sid:
            continue
        if share_used_by_other_vm(sid, vmachine_id, runtime_states):
            logger_fn(f"[CH][virtiofs] share={sid} still used by another VM; keeping daemon.")
            continue

        state_path = daemon_state_path(base_dir, sid)
        daemon = _load_daemon_state(state_path) or {}
        pid = int(daemon.get("pid") or mount.get("pid") or 0)
        socket_path = daemon.get("socket") or mount.get("socket")
        if pid > 0:
            try:
                kill_fn(pid)
                logger_fn(f"[CH][virtiofs] share={sid} daemon pid={pid} stopped.")
            except ProcessLookupError:
                logger_fn(f"[CH][virtiofs] share={sid} daemon pid={pid} already gone.")
            except Exception as e:  # noqa: BLE001 - best-effort cleanup
                logger_fn(f"[CH][virtiofs] share={sid} error stopping daemon pid={pid}: {e}")
        if socket_path:
            try:
                Path(socket_path).unlink(missing_ok=True)
            except OSError as e:
                logger_fn(f"[CH][virtiofs] share={sid} error removing socket {socket_path}: {e}")
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass

        # Only the exporting parent's departure removes the exported data.
        if owned is None or sid in owned:
            disk_dir = share_state_dir(base_dir, sid)
            try:
                shutil.rmtree(disk_dir, ignore_errors=False)
                logger_fn(f"[CH][virtiofs] share={sid} exported directory removed ({disk_dir}).")
            except FileNotFoundError:
                pass
            except OSError as e:
                logger_fn(f"[CH][virtiofs] share={sid} error removing directory {disk_dir}: {e}")
        else:
            logger_fn(f"[CH][virtiofs] share={sid} child detached; parent's data preserved.")


def _load_daemon_state(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_daemon_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    tmp.replace(path)
