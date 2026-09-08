"""Materializing a guest's shared filesystems, for whichever hypervisor boots it.

Both backends do the same thing with a service's ``shared``/``guest``
declarations -- resolve which shares this instance takes part in, reserve and
start their backends, seed a new export from the image, and inject the guest's
mount plan -- and differ only in how the resulting devices reach the guest:
cloud-hypervisor takes the ``--fs`` arguments built here, QEMU builds its own
``vhost-user-fs`` wiring from the same mount state. So the work lives here once
and each backend takes what it needs out of :class:`ShareSetup`.

Authorization is **not** decided here. ``src/manager/shares.py`` already refused
the launch if the parent does not export what the service asks for, before any MU
was spent; it is asked again on this path, from the same records, because a share
is a communication channel and a backend that simply trusts what it is handed is
a single point of failure for the one rule the node alone can enforce.

This sits above ``virtiofs.py`` on purpose. That module stays free of the node's
database and of the rootfs image, so its mounts and lifecycle can be unit-tested
on their own; the pieces that read the parent's records and the guest's image are
here.
"""
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from protos import celaut_pb2 as celaut
from src.manager.shares import authorize_shares, rundev_host_dirs
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.shared_filesystems import exported_refs
from src.virtualizers.microvm import paths, rootfs
from src.virtualizers.microvm.runtime_state import load_runtime_state
from src.virtualizers.microvm.virtiofs import (
    GUEST_MOUNT_PLAN_PATH,
    attach_virtiofs_backends,
    build_guest_mount_plan,
    load_share_state,
    mount_for,
    share_bytes,
    shared_fs_base_dir,
)

# Read under the `ch` key for both backends: it names the one virtiofsd binary
# the host has, not a per-hypervisor choice.
VIRTIOFSD_BINARY = ConfigManager().get("virtualizers.ch.VIRTIOFSD_BINARY", "virtiofsd")


class ShareSetup(NamedTuple):
    """What a VM's shared filesystems leave behind for its launch to use."""
    fs_device_args: List[str]                 # cloud-hypervisor `--fs` argv
    mounts_state: List[Dict[str, object]]     # persisted as runtime state `virtiofs`
    exported_share_ids: List[str]             # the shares this VM exports

    @property
    def any(self) -> bool:
        return bool(self.mounts_state)


NO_SHARES = ShareSetup([], [], [])


def materialize_shares(
    *,
    service: celaut.Service,
    config: Optional[celaut.Configuration],
    vmachine_id: str,
    father_id: str,
    rootfs_path: Path,
    runtime_dir: Path,
    log_prefix: str,
) -> ShareSetup:
    """Bring up every shared filesystem this instance takes part in.

    A service exports its ``shared=true`` directories to the children it launches,
    and inherits its parent's exports for its own ``guest=true`` directories. Both
    sides derive a share's id from the same parent instance id, the name the
    declaration gives it, and the name and value of its ``share_env`` variable.

    Raises ``ShareAuthorizationError`` if an inherited directory is not granted:
    ``guest`` is an execution precondition, so there is no partial start.

    Ordinary services declare neither and get :data:`NO_SHARES` -- a complete
    no-op, with nothing spawned and nothing injected into the image.
    """
    base_dir = str(shared_fs_base_dir(paths.cache_root()))
    env_values = dict(config.environment_variables) if config else {}

    exports = exported_refs(service, vmachine_id, env_values)
    inherited = authorize_shares(service=service, father_id=father_id, config=config)
    if not exports and not inherited:
        return NO_SHARES

    # A rundev sandbox exports directories of the developer's own rather than
    # anything this node materialized; they are mounted where they are.
    host_dirs = rundev_host_dirs(father_id)
    mounts = (
        [mount_for(ref, base_dir, exported=True) for ref in exports]
        + [
            mount_for(ref, base_dir, exported=False, host_dir=host_dirs.get(ref.share_id))
            for ref in inherited
        ]
    )

    log.LOGGER(
        f"{log_prefix} shared filesystems: {len(exports)} exported, "
        f"{len(inherited)} inherited from father={father_id}"
    )
    fs_device_args, mounts_state = attach_virtiofs_backends(
        mounts,
        vmachine_id,
        base_dir=base_dir,
        socket_dir=str(paths.control_socket_dir()),
        virtiofsd_binary=VIRTIOFSD_BINARY,
        # A new export starts out holding what the exporter packaged at that
        # path, read straight out of its own image, so the mount does not hide
        # the content the service shipped.
        seed_fn=lambda mount, dest: rootfs.debugfs_rdump(
            rootfs_path, mount.guest_path, dest
        ),
        logger_fn=log.LOGGER,
    )

    plan_host_path = runtime_dir / GUEST_MOUNT_PLAN_PATH.lstrip("/")
    with open(plan_host_path, "w", encoding="utf-8") as f:
        f.write(build_guest_mount_plan(mounts))
    rootfs.debugfs_write(
        image_path=rootfs_path,
        host_file=plan_host_path,
        guest_target=GUEST_MOUNT_PLAN_PATH,
    )
    log.LOGGER(
        f"{log_prefix} virtiofs devices attached: {len(mounts)}; "
        f"guest mount plan injected: {GUEST_MOUNT_PLAN_PATH}"
    )
    return ShareSetup(
        fs_device_args=fs_device_args,
        mounts_state=mounts_state,
        exported_share_ids=[ref.share_id for ref in exports],
    )


def exported_disk_bytes(vmachine_id: str) -> int:
    """Bytes the shares this VM **exports** hold on the host, or 0 if it exports none.

    A share is part of the instance that created it. Its directory is seeded from
    that instance's own image, at the path that instance declared, and only that
    instance can be there for as long as the share is -- so the storage is its
    storage, and none of it is a guest's. A guest is exactly that: it uses the
    directory while it lasts, declares no ceiling for it, and loses it when the
    exporter goes.

    That makes the whole figure the exporter's, undivided. See
    :func:`resolved_disk_bytes` for where it lands.
    """
    state = load_runtime_state(vmachine_id) or {}
    exported = state.get("exported_shares") or []
    if not exported:
        return 0
    return share_bytes(str(shared_fs_base_dir(paths.cache_root())), exported)


def resolved_disk_bytes(vmachine_id: str) -> Optional[int]:
    """What this instance actually occupies: its rootfs image plus its shares.

    ``None`` for an instance that exports nothing, which is the ordinary case and
    the one that must cost nothing to ask about -- its recorded disk is already
    exactly its image.

    A share's directory grows after the launch resolved the instance's disk, and
    an image cannot: it is a fixed-size file. So this is the one figure that has
    to be re-derived rather than trusted, and it is re-derived from the two things
    that are true right now -- the size of the image and the size of the
    directories. Keeping the instance's row on it is what makes the share count
    everywhere a row counts: in what the maintenance tick charges, in what the
    host's disk ceiling adds up (``host_limits.committed``), and in what the next
    launch is admitted against.
    """
    state = load_runtime_state(vmachine_id) or {}
    exported = state.get("exported_shares") or []
    if not exported:
        return None
    try:
        rootfs_bytes = int(Path(str(state.get("rootfs_path") or "")).stat().st_size)
    except OSError:
        return None
    return rootfs_bytes + share_bytes(
        str(shared_fs_base_dir(paths.cache_root())), exported
    )
