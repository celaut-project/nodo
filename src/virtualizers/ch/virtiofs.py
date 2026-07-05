"""
Cloud Hypervisor backend for shared-disk (virtiofs) networks.

The node-side semantics of shared-disk networks live in ``src/utils/networks.py``
(identity = the explicit ``@handle`` identity tag, virtiofs detection,
co-location placement, the ``GetNetworkInstances`` discovery rpc). This module is
the **backend** half: it actually wires the virtio-fs mount for Cloud Hypervisor
microVMs so co-located instances of a network share a directory on the host.

For each virtiofs network an instance declares we:

1. **virtiofsd daemon per shared-disk network.** Export the network's shared
   directory over a Unix socket, keyed by the network id (derived from the
   ``@handle``). One daemon per network on this host, reused by every co-located
   instance of it.
2. **virtio-fs device per guest.** Emit a ``--fs tag=<…>,socket=<…>`` device for
   the microVM so the guest can ``mount -t virtiofs <tag> <path>``. Read-only
   members mount with ``-o ro`` (see below).
3. **Anchor placement.** Drop the network's fixed ``formal`` anchor/seed blob
   into the shared directory (unchanged behavior). The anchor is DATA now, not
   identity: two instances resolve to the same network because they share the
   same ``@handle``, not because they share the anchor bytes.
4. **Lifecycle & cleanup.** Tear the daemon + socket down when the last instance
   of the network on this host goes away (reference counted from the CH runtime
   state); the shared directory (the data) is preserved.
5. **Security.** Each daemon is confined to its own directory (``--sandbox
   chroot`` by default) — deny-by-default, no cross-network access.

Read-only: expressed as a per-declaration tag on the network (see
``networks.network_is_readonly`` / ``READONLY_PROTOCOL_TAGS``), so a service can
ask for a read-only mount without any change to the proto. The single shared
daemon is always read-write; read-only is enforced on the *guest* mount, so a
read-only reader and a read-write writer share one daemon and one directory.

The pure builders at the top of this module (device arg, daemon command, path
layout, mount planning) are unit-tested directly. This host cannot run microVMs,
so the spawn/teardown orchestration is dependency-injected and exercised with
fakes rather than a live cloud-hypervisor.
"""
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional

from protos import celaut_pb2 as celaut
from src.utils.networks import (
    is_virtiofs_network,
    network_content_id,
    network_identity_handle,
    network_is_readonly,
)

# Anchor filename inside the shared directory. The anchor is the network's
# ``formal`` seed/data blob; it is written once for seeding, but network identity
# comes from the ``@handle`` tag, not the anchor (see network_content_id).
ANCHOR_FILENAME = ".celaut-network-anchor"

# Guest metadata file injected into the rootfs: a JSON list of the virtiofs
# mounts the guest init should perform ({tag, path, ro}). Kept alongside the
# other guest-injected metadata (/.__nodo_entrypoint, /etc/hosts).
GUEST_MOUNT_PLAN_PATH = "/.__nodo_virtiofs"

# Where in the guest the shared disks are mounted: /shared/<short-id>.
GUEST_MOUNT_ROOT = "/shared"


class VirtiofsMount(NamedTuple):
    """One shared-disk mount an instance participates in."""
    network_id_hex: str  # identity id (sha256 of the @handle), hex
    tag: str             # virtio-fs tag (CH --fs tag= / guest mount tag)
    readonly: bool       # this service wants the disk mounted read-only
    anchor: bytes        # Service.Network.formal seed/data blob (may be b"")
    guest_path: str      # mount point inside the guest
    disk_handle: str     # the @handle identity of the network (human-readable)


# --------------------------------------------------------------------------- #
# Pure helpers (no side effects) — directly unit-tested.
# --------------------------------------------------------------------------- #

def virtiofs_tag(network_id_hex: str) -> str:
    """Stable, short virtio-fs tag derived from the network content id."""
    return f"vfs-{network_id_hex[:16]}"


def guest_mount_point(network_id_hex: str) -> str:
    return f"{GUEST_MOUNT_ROOT}/{network_id_hex[:12]}"


def virtiofs_mounts_for_service(service: celaut.Service) -> List[VirtiofsMount]:
    """
    The shared-disk mounts a service declares, deduplicated by network id.

    A service that declares the same network read-write and read-only collapses
    to a single mount; if either declaration is read-write the mount is
    read-write (the more permissive intent wins for that instance).
    """
    by_id: Dict[str, VirtiofsMount] = {}
    for network in service.network:
        if not is_virtiofs_network(network):
            continue
        # network_content_id raises ValueError on an invalid @handle declaration
        # (zero or >1 distinct handles); an invalid virtiofs network is rejected.
        handle = network_identity_handle(network)
        nid_hex = network_content_id(network).hex()
        readonly = network_is_readonly(network)
        anchor = bytes(network.formal or b"")
        existing = by_id.get(nid_hex)
        if existing is not None:
            by_id[nid_hex] = existing._replace(
                readonly=existing.readonly and readonly,
                anchor=existing.anchor or anchor,
            )
            continue
        by_id[nid_hex] = VirtiofsMount(
            network_id_hex=nid_hex,
            tag=virtiofs_tag(nid_hex),
            readonly=readonly,
            anchor=anchor,
            guest_path=guest_mount_point(nid_hex),
            disk_handle=handle,
        )
    return list(by_id.values())


def network_state_dir(base_dir: str, network_id_hex: str) -> Path:
    """Per-network host directory: holds the shared export + daemon state."""
    return Path(base_dir) / network_id_hex


def shared_dir(base_dir: str, network_id_hex: str) -> Path:
    """The directory exported to the guests of a network (the shared disk)."""
    return network_state_dir(base_dir, network_id_hex) / "shared"


def daemon_state_path(base_dir: str, network_id_hex: str) -> Path:
    return network_state_dir(base_dir, network_id_hex) / "daemon.json"


def shared_dir_usage_bytes(base_dir: str, network_id_hex: str) -> int:
    """
    Measured disk usage (bytes) of a network's shared directory — the ``du`` of
    the shared disk. This is the number attributed to the network's *origin*
    service and billed against its declared ``Sysresources.disk_space``.

    Missing directory (never created / already torn down) counts as zero.
    """
    root = shared_dir(base_dir, network_id_hex)
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            file_path = Path(dirpath) / name
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def attributed_shared_disk_usage_bytes(
    network_ids,
    *,
    base_dir: str,
    declared_disk_space: Optional[int] = None,
) -> int:
    """
    Measured usage (bytes) of the given networks' shared directories, clamped to
    the origin service's declared ``disk_space``.

    Josemi's rule: a shared disk's size lives *within* the disk the origin
    service already declares in ``Sysresources.disk_space`` and is billed from
    there — there is no separate size/quota field on the ``Network`` proto. The
    cap and the billing source are the same existing number. Metering is
    measured (``du``), not reserved: empty shared disks cost nothing; ones that
    exceed the declaration are charged exactly the declaration.

    ``declared_disk_space`` of ``None`` or ``0`` means "no declared cap" and the
    raw measured usage is returned.
    """
    total = 0
    for nid in network_ids:
        total += shared_dir_usage_bytes(base_dir, nid)
    if declared_disk_space is not None and int(declared_disk_space) > 0:
        total = min(total, int(declared_disk_space))
    return total


def virtiofs_socket_path(socket_dir: str, network_id_hex: str) -> Path:
    """
    virtiofsd control socket. Kept in the (short) CH API socket dir to stay
    under the AF_UNIX SUN_LEN limit, mirroring the API socket convention.
    """
    return Path(socket_dir) / f"vfs-{network_id_hex[:16]}.sock"


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
    """
    Command line for the rust ``virtiofsd`` daemon exporting ``export_dir``.

    The daemon is always read-write and confined to ``export_dir`` via
    ``--sandbox`` (deny-by-default, no cross-network access). Read-only members
    mount the resulting device with ``-o ro`` on the guest side, so a single
    daemon serves both read-write and read-only participants.
    """
    return [
        binary,
        "--socket-path", str(socket_path),
        "--shared-dir", str(export_dir),
        "--sandbox", sandbox,
        "--cache", cache,
    ]


def build_guest_mount_plan(mounts: List[VirtiofsMount]) -> str:
    """JSON the guest init consumes to mount each shared disk (ro where asked)."""
    return json.dumps(
        [
            {"tag": m.tag, "path": m.guest_path, "ro": m.readonly}
            for m in mounts
        ],
        sort_keys=True,
    )


def network_used_by_other_vm(
    network_id_hex: str,
    self_vmachine_id: str,
    runtime_states: Dict[str, dict],
) -> bool:
    """
    Reference count: does any OTHER live CH VM still use this network?

    ``runtime_states`` is ``{vmachine_id: state}`` (see
    ``runtime_state.list_runtime_states``); each state's ``virtiofs`` field is
    the list of mounts persisted by :func:`attach_virtiofs_backends`.
    """
    for vmachine_id, state in runtime_states.items():
        if vmachine_id == self_vmachine_id:
            continue
        for mount in state.get("virtiofs") or []:
            if mount.get("network_id_hex") == network_id_hex:
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


def ensure_network_backend(
    mount: VirtiofsMount,
    *,
    base_dir: str,
    socket_dir: str,
    virtiofsd_binary: str,
    sandbox: str = "chroot",
    spawn_fn: Callable[[List[str], Path], int] = _default_spawn,
    pid_alive_fn: Callable[[int], bool] = _default_pid_alive,
    on_first_create: Optional[Callable[[str], None]] = None,
    logger_fn: Callable[[str], None] = lambda _m: None,
) -> Dict[str, object]:
    """
    Make sure a virtiofsd daemon is running for ``mount``'s network and return
    the state needed to attach a guest and, later, tear it down.

    Idempotent: if a daemon for this network is already alive with its socket
    present, it is reused (co-located instances share one daemon + directory).

    ``on_first_create`` — if given — is invoked with the network id (hex) exactly
    once, the first time the shared directory is created on this host (i.e. this
    instance is the *origin* of the network). It is NOT called for later
    instances that merely join an already-created shared disk, so the origin can
    be persisted idempotently by the caller.
    """
    nid = mount.network_id_hex
    handle = mount.disk_handle
    export_dir = shared_dir(base_dir, nid)
    socket_path = virtiofs_socket_path(socket_dir, nid)
    state_path = daemon_state_path(base_dir, nid)

    # "First create" == the shared directory did not exist before this call.
    # A preserved disk (delete_disk_on_last=False) re-joined by a later instance
    # already exists, so its original origin is kept.
    first_create = not export_dir.exists()
    export_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(network_state_dir(base_dir, nid), 0o700)
    os.chmod(export_dir, 0o700)
    Path(socket_dir).mkdir(parents=True, exist_ok=True)

    if first_create and on_first_create is not None:
        on_first_create(nid)

    # Anchor placement: establish membership by writing the ABCD blob once.
    if mount.anchor:
        anchor_path = export_dir / ANCHOR_FILENAME
        if not anchor_path.exists():
            with open(anchor_path, "wb") as f:
                f.write(mount.anchor)
            logger_fn(f"[CH][virtiofs] network={nid} anchor placed ({len(mount.anchor)} bytes)")

    # Reuse an existing healthy daemon if present.
    existing = _load_daemon_state(state_path)
    if existing and pid_alive_fn(int(existing.get("pid") or 0)) and socket_path.exists():
        logger_fn(f"[CH][virtiofs] network={nid} reusing daemon pid={existing.get('pid')}")
        return {
            "network_id_hex": nid,
            "disk_handle": handle,
            "tag": mount.tag,
            "socket": str(socket_path),
            "shared_dir": str(export_dir),
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
    log_path = network_state_dir(base_dir, nid) / "virtiofsd.log"
    logger_fn(f"[CH][virtiofs] network={handle} ({nid}) starting daemon: {' '.join(command)}")
    pid = spawn_fn(command, log_path)

    _save_daemon_state(
        state_path,
        {
            "network_id_hex": nid,
            "pid": pid,
            "socket": str(socket_path),
            "shared_dir": str(export_dir),
        },
    )
    return {
        "network_id_hex": nid,
        "disk_handle": handle,
        "tag": mount.tag,
        "socket": str(socket_path),
        "shared_dir": str(export_dir),
        "pid": pid,
        "readonly": mount.readonly,
        "guest_path": mount.guest_path,
    }


def attach_virtiofs_backends(
    service: celaut.Service,
    *,
    base_dir: str,
    socket_dir: str,
    virtiofsd_binary: str,
    sandbox: str = "chroot",
    spawn_fn: Callable[[List[str], Path], int] = _default_spawn,
    pid_alive_fn: Callable[[int], bool] = _default_pid_alive,
    on_first_create: Optional[Callable[[str], None]] = None,
    logger_fn: Callable[[str], None] = lambda _m: None,
):
    """
    Ensure every shared-disk network the service declares has a running backend.

    Returns ``(fs_device_args, mounts_state, mounts)``:
      * ``fs_device_args``: flat argv to splice into the cloud-hypervisor
        command (``["--fs", "<arg>", "--fs", "<arg>", …]``), one per network.
      * ``mounts_state``: JSON-serializable list persisted in the VM runtime
        state (drives reference-counted teardown).
      * ``mounts``: the :class:`VirtiofsMount` list (for the guest mount plan).

    A service that declares no virtiofs network yields empty lists — a complete
    no-op for ordinary services.
    """
    mounts = virtiofs_mounts_for_service(service)
    fs_device_args: List[str] = []
    mounts_state: List[Dict[str, object]] = []
    for mount in mounts:
        backend = ensure_network_backend(
            mount,
            base_dir=base_dir,
            socket_dir=socket_dir,
            virtiofsd_binary=virtiofsd_binary,
            sandbox=sandbox,
            spawn_fn=spawn_fn,
            pid_alive_fn=pid_alive_fn,
            on_first_create=on_first_create,
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
    delete_disk_on_last: bool = True,
    kill_fn: Callable[[int], None] = lambda pid: os.kill(pid, signal.SIGTERM),
    on_disk_deleted: Optional[Callable[[str], None]] = None,
    logger_fn: Callable[[str], None] = lambda _m: None,
) -> None:
    """
    Release the virtiofs backends a VM used; stop a daemon only when this was the
    last instance of its network on the host.

    When this was the last instance of a network and ``delete_disk_on_last`` is
    True (the default), the network's shared disk directory — the exported data
    plus its anchor and daemon state — is removed from the server. Set
    ``delete_disk_on_last`` to False to preserve the shared disk for reuse by a
    future instance of the same network.

    ``runtime_states`` must be the CH runtime states with ``vmachine_id`` ALREADY
    removed (i.e. the states of the *other* live VMs), so the reference count
    does not see the VM being torn down.
    """
    for mount in mounts_state or []:
        nid = mount.get("network_id_hex")
        if not nid:
            continue
        if network_used_by_other_vm(nid, vmachine_id, runtime_states):
            logger_fn(
                f"[CH][virtiofs] network={nid} still used by another VM; "
                "keeping daemon."
            )
            continue

        state_path = daemon_state_path(base_dir, nid)
        daemon = _load_daemon_state(state_path) or {}
        pid = int(daemon.get("pid") or mount.get("pid") or 0)
        socket_path = daemon.get("socket") or mount.get("socket")
        if pid > 0:
            try:
                kill_fn(pid)
                logger_fn(f"[CH][virtiofs] network={nid} daemon pid={pid} stopped.")
            except ProcessLookupError:
                logger_fn(f"[CH][virtiofs] network={nid} daemon pid={pid} already gone.")
            except Exception as e:  # noqa: BLE001 - best-effort cleanup
                logger_fn(f"[CH][virtiofs] network={nid} error stopping daemon pid={pid}: {e}")
        if socket_path:
            try:
                Path(socket_path).unlink(missing_ok=True)
            except OSError as e:
                logger_fn(f"[CH][virtiofs] network={nid} error removing socket {socket_path}: {e}")
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass

        # Last instance of this network on the host: optionally remove the shared
        # disk from the server. Default is to delete; keeping it lets a later
        # instance of the same network re-attach the existing data.
        if delete_disk_on_last:
            disk_dir = network_state_dir(base_dir, nid)
            try:
                shutil.rmtree(disk_dir, ignore_errors=False)
                logger_fn(f"[CH][virtiofs] network={nid} shared disk removed ({disk_dir}).")
                # The disk is gone: forget its origin mapping so a future
                # re-creation records a fresh origin service.
                if on_disk_deleted is not None:
                    on_disk_deleted(nid)
            except FileNotFoundError:
                if on_disk_deleted is not None:
                    on_disk_deleted(nid)
            except OSError as e:
                logger_fn(f"[CH][virtiofs] network={nid} error removing shared disk {disk_dir}: {e}")
        else:
            logger_fn(f"[CH][virtiofs] network={nid} shared disk preserved (delete_disk_on_last=False).")


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
