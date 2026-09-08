"""VirtioFS backend for shared filesystems (parent -> child inheritance).

The *semantics* of shared filesystems live in ``src/utils/shared_filesystems.py``
and their *authorization* in ``src/manager/shares.py``. This module is the
**backend**: it wires a virtio-fs mount for a microVM guest so a child instance
reaches a directory exported by the parent that launched it. It decides nothing
about who may reach what -- it is handed the shares that were already
authorized.

For each share (identified by ``shared_filesystems.share_id``) it provides:

1. **A daemon per share.** virtiofsd exports the share's host directory over a
   Unix socket keyed by the share id. One daemon per share on this host, reused
   by the exporting parent and every child that inherits it.
2. **A device per guest.** A ``--fs tag=<…>,socket=<…>`` device (cloud-hypervisor)
   or the same socket wired as ``vhost-user-fs`` (QEMU), so the guest can
   ``mount -t virtiofs <tag> <declared-path>``. A child that asked for
   ``access=ro`` mounts with ``-o ro``.
3. **Seeding.** The first time an export is materialized its directory is filled
   with what the exporter packaged at that path, so the mount does not hide the
   content the service shipped.
4. **Lifecycle by reservation.** A share's own state file records who is using it,
   written *before* the VM that will use it is built. Ownership lives there and
   not in the state of whichever VM happens to leave last, so neither of the two
   ways a share used to be lost can happen: a parent tearing down while its child
   is still starting, or a directory outliving everyone with nobody left to
   delete it.
5. **Security.** Each daemon is confined to its own directory (``--sandbox
   chroot`` by default): deny-by-default, no cross-share access.

VirtioFS is purely an implementation detail here; the service spec never mentions
it. The pure builders are unit-tested directly; this host cannot run microVMs, so
the spawn/teardown orchestration is dependency-injected and exercised with fakes.
"""
import json
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional

from src.utils.shared_filesystems import ShareRef
from src.virtualizers.microvm import paths

# Guest metadata file injected into the rootfs: a JSON list of the virtiofs
# mounts the guest init should perform ({tag, path, ro}). Kept alongside the
# other guest-injected metadata.
GUEST_MOUNT_PLAN_PATH = "/.__nodo_virtiofs"

_STATE_LOCKS: Dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


class SharedMount(NamedTuple):
    """One shared-filesystem mount a VM participates in."""
    share_id_hex: str    # identity of the share (see shared_filesystems.share_id)
    tag: str             # virtio-fs tag (CH --fs tag= / guest mount tag)
    readonly: bool       # mount read-only in this guest
    guest_path: str      # where the directory is mounted inside the guest
    host_dir: str        # the host directory backing the share
    exported: bool = False  # this VM is the share's exporter, not a guest of it
    external: bool = False  # the host directory is someone else's (rundev): never
                            # seeded, never deleted


# --------------------------------------------------------------------------- #
# Pure helpers (no side effects) — directly unit-tested.
# --------------------------------------------------------------------------- #

def shared_fs_base_dir(cache: str) -> Path:
    """Host directory that backs every share on this node.

    A single location so the launcher (which materializes shares) and the killer
    (which releases them) always agree. ``cache`` is taken as an argument rather
    than read from config so callers that already hold a temporary root -- the
    tests, and any caller working off a copy -- can point the whole share tree
    somewhere else.
    """
    return Path(cache) / paths.FAMILY_DIR_NAME / "shared_fs"


def virtiofs_tag(share_id_hex: str) -> str:
    """Stable, short virtio-fs tag derived from the share id."""
    return f"vfs-{share_id_hex[:16]}"


def share_state_dir(base_dir: str, share_id_hex: str) -> Path:
    """Per-share host directory: holds the exported data + the share's state."""
    return Path(base_dir) / share_id_hex


def shared_dir(base_dir: str, share_id_hex: str) -> Path:
    """The host directory exported to the guests of a share."""
    return share_state_dir(base_dir, share_id_hex) / "shared"


def share_state_path(base_dir: str, share_id_hex: str) -> Path:
    return share_state_dir(base_dir, share_id_hex) / "share.json"


def virtiofs_socket_path(socket_dir: str, share_id_hex: str) -> Path:
    """virtiofsd control socket, kept in the (short) control socket dir to stay
    under the AF_UNIX SUN_LEN limit."""
    return Path(socket_dir) / f"vfs-{share_id_hex[:16]}.sock"


def mount_for(
    ref: ShareRef,
    base_dir: str,
    *,
    exported: bool,
    host_dir: Optional[str] = None,
) -> SharedMount:
    """The mount that realizes one authorized share for this VM.

    An exporter always mounts read-write, whatever the declaration asked for:
    writes have to land on the host directory its children will read. ``host_dir``
    overrides where the share lives, which is how a rundev sandbox hands over a
    directory of the developer's own.
    """
    return SharedMount(
        share_id_hex=ref.share_id,
        tag=virtiofs_tag(ref.share_id),
        readonly=False if exported else ref.readonly,
        guest_path=ref.path,
        host_dir=host_dir or str(shared_dir(base_dir, ref.share_id)),
        exported=exported,
        external=bool(host_dir),
    )


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


# --------------------------------------------------------------------------- #
# Share state: who is using a share, recorded in the share's own file.
# --------------------------------------------------------------------------- #

def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCK_GUARD:
        if key not in _STATE_LOCKS:
            _STATE_LOCKS[key] = threading.Lock()
        return _STATE_LOCKS[key]


def load_share_state(base_dir: str, share_id_hex: str) -> Optional[dict]:
    path = share_state_path(base_dir, share_id_hex)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_share_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    tmp.replace(path)


def reserve_share(
    base_dir: str, mount: SharedMount, vmachine_id: str
) -> dict:
    """Record that ``vmachine_id`` is using this share, and return its state.

    Called while the VM is still being built, before its hypervisor process
    exists. That is the point: a child that reserved its share cannot have the
    directory pulled out from under it by a parent that tears down in the
    meantime, which is what happened while the reference count was derived from
    the live VMs -- a starting child was in none of them yet.
    """
    path = share_state_path(base_dir, mount.share_id_hex)
    with _lock_for(path):
        state = load_share_state(base_dir, mount.share_id_hex) or {
            "share_id_hex": mount.share_id_hex,
            "users": [],
            "external": mount.external,
            "host_dir": mount.host_dir,
        }
        users = [u for u in state.get("users") or [] if u != vmachine_id]
        users.append(vmachine_id)
        state["users"] = users
        if mount.exported:
            state["owner"] = vmachine_id
        _save_share_state(path, state)
        return state


def release_share(base_dir: str, share_id_hex: str, vmachine_id: str) -> dict:
    """Drop ``vmachine_id`` from a share's users and say whether the share is over.

    Returns the state with a ``spent`` flag: true when the share has to be taken
    down, which happens on either of two events.

    **Its exporter left.** A share is part of the instance that created it: its
    directory is that instance's storage, seeded from its own image, counted in
    its disk and charged to it. Nothing is left to hold it up or pay for it once
    that instance is gone, so it goes with it -- children that were guests of it
    lose the directory, which is the whole of what being a guest means.

    **Or its last user left.** The exporter may have gone first, in which case
    what remains is a directory nobody owns; it is removed when the last VM
    holding it departs so nothing outlives everyone.
    """
    path = share_state_path(base_dir, share_id_hex)
    with _lock_for(path):
        state = load_share_state(base_dir, share_id_hex) or {"users": []}
        users = [u for u in state.get("users") or [] if u != vmachine_id]
        state["users"] = users
        state["spent"] = (state.get("owner") == vmachine_id) or not users
        if state["spent"]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            _save_share_state(path, state)
        return state


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
    vmachine_id: str,
    *,
    base_dir: str,
    socket_dir: str,
    virtiofsd_binary: str,
    sandbox: str = "chroot",
    spawn_fn: Callable[[List[str], Path], int] = _default_spawn,
    pid_alive_fn: Callable[[int], bool] = _default_pid_alive,
    seed_fn: Optional[Callable[[SharedMount, Path], None]] = None,
    logger_fn: Callable[[str], None] = lambda _m: None,
) -> Dict[str, object]:
    """Reserve ``mount``'s share for this VM, make sure its daemon is running, and
    return the state needed to attach the guest and, later, release it.

    Idempotent: a daemon already alive with its socket present is reused, so the
    exporting parent and every co-located child share a single daemon and a
    single host directory.

    ``seed_fn(mount, export_dir)`` fills the directory the first time an export
    is materialized. It runs before the daemon binds, only for the exporter, and
    never for a directory the node does not own (a rundev sandbox's).
    """
    sid = mount.share_id_hex
    export_dir = Path(mount.host_dir)
    socket_path = virtiofs_socket_path(socket_dir, sid)

    first_materialization = not export_dir.exists()
    export_dir.mkdir(parents=True, exist_ok=True)
    if not mount.external:
        os.chmod(share_state_dir(base_dir, sid), 0o700)
        os.chmod(export_dir, 0o700)
    Path(socket_dir).mkdir(parents=True, exist_ok=True)

    if first_materialization and mount.exported and not mount.external and seed_fn:
        logger_fn(f"[virtiofs] share={sid} seeding from packaged {mount.guest_path}")
        seed_fn(mount, export_dir)

    state = reserve_share(base_dir, mount, vmachine_id)
    attached = {
        "share_id_hex": sid,
        "tag": mount.tag,
        "socket": str(socket_path),
        "host_dir": str(export_dir),
        "readonly": mount.readonly,
        "guest_path": mount.guest_path,
        "external": mount.external,
    }

    # Reuse an existing healthy daemon if present.
    pid = int(state.get("pid") or 0)
    if pid and pid_alive_fn(pid) and socket_path.exists():
        logger_fn(f"[virtiofs] share={sid} reusing daemon pid={pid}")
        return {**attached, "pid": pid}

    # Stale socket from a dead daemon would block bind — clear it.
    try:
        socket_path.unlink(missing_ok=True)
    except OSError:
        pass

    command = build_virtiofsd_command(
        virtiofsd_binary, socket_path, export_dir, sandbox=sandbox
    )
    log_path = share_state_dir(base_dir, sid) / "virtiofsd.log"
    logger_fn(f"[virtiofs] share={sid} starting daemon: {' '.join(command)}")
    pid = spawn_fn(command, log_path)

    path = share_state_path(base_dir, sid)
    with _lock_for(path):
        stored = load_share_state(base_dir, sid) or state
        stored.update({"pid": pid, "socket": str(socket_path)})
        _save_share_state(path, stored)
    return {**attached, "pid": pid}


def attach_virtiofs_backends(
    mounts: List[SharedMount],
    vmachine_id: str,
    *,
    base_dir: str,
    socket_dir: str,
    virtiofsd_binary: str,
    sandbox: str = "chroot",
    spawn_fn: Callable[[List[str], Path], int] = _default_spawn,
    pid_alive_fn: Callable[[int], bool] = _default_pid_alive,
    seed_fn: Optional[Callable[[SharedMount, Path], None]] = None,
    logger_fn: Callable[[str], None] = lambda _m: None,
):
    """Ensure every shared filesystem in ``mounts`` has a running backend.

    Returns ``(fs_device_args, mounts_state)``:
      * ``fs_device_args``: flat argv to splice into the cloud-hypervisor command
        (``["--fs", "<arg>", …]``), one per share.
      * ``mounts_state``: JSON-serializable list persisted in the VM runtime
        state, which is what its teardown later releases.

    A VM with no shared filesystems yields empty lists — a complete no-op for
    ordinary services.
    """
    fs_device_args: List[str] = []
    mounts_state: List[Dict[str, object]] = []
    for mount in mounts:
        backend = ensure_share_backend(
            mount,
            vmachine_id,
            base_dir=base_dir,
            socket_dir=socket_dir,
            virtiofsd_binary=virtiofsd_binary,
            sandbox=sandbox,
            spawn_fn=spawn_fn,
            pid_alive_fn=pid_alive_fn,
            seed_fn=seed_fn,
            logger_fn=logger_fn,
        )
        fs_device_args.extend(
            ["--fs", build_fs_device_arg(mount.tag, backend["socket"])]
        )
        mounts_state.append(backend)
    return fs_device_args, mounts_state


def teardown_virtiofs_for_vm(
    vmachine_id: str,
    mounts_state: List[dict],
    *,
    base_dir: str,
    kill_fn: Callable[[int], None] = lambda pid: os.kill(pid, signal.SIGTERM),
    logger_fn: Callable[[str], None] = lambda _m: None,
) -> None:
    """Release the shares a VM used, taking down the ones that are over.

    The share's own state decides, and a share ends when its **exporter** leaves
    -- it is part of that instance's storage, so nothing is left to hold it or
    pay for it -- or, if the exporter is already gone, when its last user does.
    A directory the node does not own (a rundev sandbox's) is released but never
    deleted.
    """
    for mount in mounts_state or []:
        sid = mount.get("share_id_hex")
        if not sid:
            continue
        state = release_share(base_dir, sid, vmachine_id)
        if not state.get("spent"):
            logger_fn(
                f"[virtiofs] share={sid} still used by "
                f"{len(state.get('users') or [])} VM(s); keeping daemon."
            )
            continue
        if state.get("users"):
            logger_fn(
                f"[virtiofs] share={sid} exporter left; taking it down with "
                f"{len(state['users'])} guest(s) still running, which lose the directory."
            )

        pid = int(state.get("pid") or mount.get("pid") or 0)
        socket_path = state.get("socket") or mount.get("socket")
        if pid > 0:
            try:
                kill_fn(pid)
                logger_fn(f"[virtiofs] share={sid} daemon pid={pid} stopped.")
            except ProcessLookupError:
                logger_fn(f"[virtiofs] share={sid} daemon pid={pid} already gone.")
            except Exception as e:  # noqa: BLE001 - best-effort cleanup
                logger_fn(f"[virtiofs] share={sid} error stopping daemon pid={pid}: {e}")
        if socket_path:
            try:
                Path(socket_path).unlink(missing_ok=True)
            except OSError as e:
                logger_fn(f"[virtiofs] share={sid} error removing socket {socket_path}: {e}")

        if state.get("external") or mount.get("external"):
            logger_fn(f"[virtiofs] share={sid} released; external directory kept.")
            continue
        disk_dir = share_state_dir(base_dir, sid)
        try:
            shutil.rmtree(disk_dir, ignore_errors=False)
            logger_fn(f"[virtiofs] share={sid} directory removed ({disk_dir}).")
        except FileNotFoundError:
            pass
        except OSError as e:
            logger_fn(f"[virtiofs] share={sid} error removing directory {disk_dir}: {e}")


def share_bytes(base_dir: str, share_ids: List[str]) -> int:
    """Bytes the given shares hold on this host.

    A share lives outside every rootfs, so its writes are invisible to the disk a
    VM is accounted for unless they are counted here. See
    ``shares.exported_disk_bytes`` for whose disk they belong to.
    """
    total = 0
    for sid in share_ids or []:
        for path in shared_dir(base_dir, sid).rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total
