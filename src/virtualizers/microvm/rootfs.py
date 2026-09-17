"""Putting the node's data inside the guest's own filesystem, offline.

Every read of and write into a guest image goes through ``debugfs``, never a loop
mount: that is what lets a node build and launch guests without root and without
``CAP_SYS_ADMIN`` (see ``docs/ROOTLESS.md``). Both hypervisors inject the same
three things into the same image the same way -- the serialized
``ConfigurationFile`` at the path the service declared, the resolved entrypoint,
and (when there are shares) the virtiofs mount plan -- and pull a share's seed
data back out of it the same way, so this is one implementation, not a convention
two backends each re-implement.
"""
import posixpath
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.gateway.utils import generate_node_peer_info, peer_gateway_instance
from src.manager.networks import filter_networks_with_ancestors, resolve_network
from src.utils import logger as log
from src.utils.network_policy import enforce_network_policy
from src.virtualizers.microvm.errors import MicroVMError
from src.virtualizers.microvm.host import run
from src.virtualizers.microvm.network import NETWORK_BRIDGE_NAME

sc = SQLConnection()

# The serialized configuration goes to the filesystem root, deterministically,
# whatever `service.container.config_declaration.path` says: the guest's own
# /init reads it from there.
GUEST_CONFIG_TARGETS = ["/__config__"]

GUEST_ENTRYPOINT_PATH = "/.__nodo_entrypoint"

# The metadata disk: a second virtio-blk device carrying exactly the per-instance
# files the node would otherwise write into the rootfs image itself.
#
# It exists because those writes are offline ext4 writes (``debugfs_write``), and
# a ``read_mode=ro`` service's image is squashfs or erofs -- formats with no
# writer at all, in debugfs or anywhere else. The content is unchanged and so are
# the paths the guest reads them at; only the carrier differs, and only for ro.
#
# ext4 rather than a second squashfs/erofs: this image is written per launch, by
# ``mkfs.ext4 -d``, which is the one image tool the node already requires of every
# host for the ordinary path (and which ``nodo doctor`` already checks for). A ro
# service must not additionally require the host to have a read-only mkfs tool for
# something the node generates itself.
METADATA_DISK_NAME = "metadata.ext4"
GUEST_METADATA_MOUNT = "/.__nodo_meta"

# Sized for a handful of small files: a serialized ConfigurationFile, one line of
# entrypoint, and a virtiofs mount plan. 4 MiB is far above any of them and still
# below the noise floor of a guest's memory, and the image is sparse besides.
METADATA_DISK_BYTES = 4 * 1024 * 1024


def guest_config_targets(service: celaut.Service) -> List[str]:
    _ = service
    return list(GUEST_CONFIG_TARGETS)


class GuestMetadata:
    """Where this launch's per-instance files go, for either kind of rootfs.

    The node delivers three things to every guest -- the serialized
    ``ConfigurationFile``, the resolved entrypoint, and (when there are shares) the
    virtiofs mount plan -- and the guest reads all three at fixed absolute paths.
    Those paths do not change here. Only the carrier does:

    * a **writable** rootfs takes them the way it always has, written into the
      offline ext4 image with ``debugfs``;
    * a **read-only** one cannot be written at all, by debugfs or anything else,
      so they are packed into a small ext4 metadata disk attached as a second
      virtio-blk device and copied onto the guest's overlay by ``/init``.

    One object so the launch path states each file once and neither backend grows
    an ``if read_only`` around every injection.
    """

    def __init__(self, image_path: Path, runtime_dir: Path, read_only: bool):
        self._image_path = image_path
        self._runtime_dir = runtime_dir
        self._read_only = read_only
        self._staged: Dict[str, Path] = {}

    @property
    def read_only(self) -> bool:
        return self._read_only

    def put(self, host_file: Path, guest_target: str) -> None:
        """Deliver ``host_file`` to the guest at ``guest_target``."""
        if self._read_only:
            self._staged[guest_target] = host_file
            return
        debugfs_write(
            image_path=self._image_path,
            host_file=host_file,
            guest_target=guest_target,
        )

    def finalize(self) -> Optional[Path]:
        """Build the metadata disk, or ``None`` when the image took the files.

        Called once, after every ``put``, because the disk is written whole: it is
        an ext4 image populated by ``mkfs.ext4 -d`` from a staging directory, not a
        filesystem appended to.
        """
        if not self._read_only:
            return None
        return build_metadata_disk(self._runtime_dir, self._staged)


def build_metadata_disk(runtime_dir: Path, entries: Dict[str, Path]) -> Path:
    """Pack ``entries`` (guest path -> host file) into this VM's metadata disk.

    Replaces the ``debugfs_write`` calls for a read-only rootfs, and nothing else:
    the guest still finds ``/__config__`` at ``/__config__``, because /init copies
    what lands here onto the overlay before ``switch_root`` (see
    ``bash/build_ch_initramfs.sh``).

    Rebuilt from scratch on every call rather than updated, since it is a launch
    artifact in the VM's own runtime directory and there is never a previous one
    worth keeping.
    """
    image_path = runtime_dir / METADATA_DISK_NAME
    if image_path.exists():
        image_path.unlink()

    with tempfile.TemporaryDirectory(dir=str(runtime_dir)) as staging:
        staging_dir = Path(staging)
        for guest_path, host_file in entries.items():
            # Flat by construction: every guest path this carries is a file at the
            # root of the guest filesystem, and keeping it flat means no path
            # arithmetic on a string that came from a service specification.
            name = posixpath.basename(guest_path)
            if not name or name != guest_path.lstrip("/"):
                raise MicroVMError(
                    f"metadata disk entries must be files at the guest root, got '{guest_path}'"
                )
            shutil.copyfile(host_file, staging_dir / name)

        run(
            [
                "mkfs.ext4",
                "-b",
                "4096",
                "-m",
                "0",
                "-d",
                str(staging_dir),
                str(image_path),
                str(METADATA_DISK_BYTES // 4096),
            ]
        )

    return image_path


def debugfs_write(image_path: Path, host_file: Path, guest_target: str) -> None:
    """Write ``host_file`` into an offline ext4 image at ``guest_target``.

    Parent directories are created one level at a time because ``debugfs mkdir``
    has no ``-p``; an existing directory is not an error. The target is removed
    before the write so a second injection replaces rather than appends.
    """
    guest_target = guest_target if guest_target.startswith("/") else f"/{guest_target}"
    target_dir = posixpath.dirname(guest_target)

    directory_parts = [part for part in target_dir.split("/") if part]
    current = ""
    for part in directory_parts:
        current = f"{current}/{part}"
        mkdir_result = run(
            ["debugfs", "-w", "-R", f"mkdir {current}", str(image_path)],
            check=False,
        )
        if mkdir_result.returncode != 0:
            stderr = (mkdir_result.stderr or "").strip().lower()
            if "file exists" not in stderr:
                raise MicroVMError(
                    f"debugfs mkdir failed for {current}: {mkdir_result.stderr or mkdir_result.stdout or ''}"
                )

    run(["debugfs", "-w", "-R", f"rm {guest_target}", str(image_path)], check=False)

    write_cmd = f"write {host_file} {guest_target}"
    run(["debugfs", "-w", "-R", write_cmd, str(image_path)])


def debugfs_rdump(image_path: Path, guest_dir: str, host_dest: Path) -> bool:
    """Copy the offline image's ``guest_dir`` subtree out onto ``host_dest``.

    The read half of ``debugfs_write``, and rootless for the same reason: no loop
    mount, no ``CAP_SYS_ADMIN``. Used to seed a shared filesystem's host
    directory with what the exporting service packaged at that path, so mounting
    the share over it does not hide the packaged content.

    ``debugfs rdump`` writes the source *directory* into its destination, so it
    dumps into a staging directory next to the target and the entries are then
    moved across. What landed in the staging directory is discovered by reading
    it, never by deriving a name from ``guest_dir``: that string comes from the
    service specification, and a path arithmetic on it is a path arithmetic on
    someone else's input. Returns False (having logged) when the image has
    nothing at that path, which is an empty share, not an error.
    """
    host_dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=host_dest.parent) as staging:
        result = run(
            ["debugfs", "-R", f"rdump {guest_dir} {staging}", str(image_path)],
            check=False,
        )
        dumped = next((p for p in Path(staging).iterdir() if p.is_dir()), None)
        if dumped is None:
            log.LOGGER(
                f"nothing to seed from {image_path}:{guest_dir} "
                f"({(result.stderr or result.stdout or '').strip()})"
            )
            return False
        for entry in dumped.iterdir():
            shutil.move(str(entry), str(host_dest / entry.name))
    return True


def build_network_resolution(
    service: celaut.Service,
    father_id: str,
    config: Optional[celaut.Configuration] = None,
) -> List[celaut.ConfigurationFile.NetworkResolution]:
    networks = service.network
    if father_id and sc.internal_instance_exists(id=father_id):
        networks = filter_networks_with_ancestors(networks=networks, father_id=father_id)

    # Defence in depth for the operator's network policy (#280). The launcher and the
    # cost path already refuse a service whose declaration the policy rejects, so
    # what is judged here is the narrower set that survived the ancestor chain --
    # what is actually about to be opened. It aborts the launch instead of dropping
    # the network, because reaching this line at all means an earlier check did not
    # run, and a guest silently started without the egress it asked for is the
    # unexplained rejection this policy exists to replace.
    enforce_network_policy(networks=networks, subject="this instance")

    # The requesting instance's own environment values drive Network peer
    # filtering (Service.Network.environment_variable).
    requester_env_values = dict(config.environment_variables) if config else None

    return [
        celaut.ConfigurationFile.NetworkResolution(
            tags=network.tags,
            peer_instances=resolve_network(network, requester_env_values=requester_env_values),
        )
        for network in networks
        if len(network.tags) > 0
    ]


def build_configuration_file(
    config: Optional[celaut.Configuration],
    resources: celaut.Sysresources,
    network_resolution: List[celaut.ConfigurationFile.NetworkResolution],
) -> celaut.ConfigurationFile:
    cfg = celaut.ConfigurationFile()
    local_peer = generate_node_peer_info(network=NETWORK_BRIDGE_NAME)
    cfg.gateway.CopyFrom(peer_gateway_instance(local_peer))

    if config:
        cfg.config.CopyFrom(config)

    if network_resolution:
        cfg.network_resolution.extend(network_resolution)

    if resources:
        cfg.initial_sysresources.CopyFrom(resources)

    return cfg


def runtime_disk_bytes(log_prefix: str, rootfs_path: Path) -> int:
    """Bytes of disk this instance actually holds: the size of its own rootfs image.

    Each instance gets a private copy of the service's rootfs (``shutil.copy2`` into
    its runtime dir), so the image's size is what the node has committed on its
    behalf, whatever the manifest asked for.

    Returns 0 if the image cannot be stat'd, which the launcher reads as "the
    virtualizer did not resolve disk" and falls back to the manifest for -- never
    persisting a zero, since that would bill the instance no disk at all.
    """
    try:
        return int(rootfs_path.stat().st_size)
    except OSError as e:
        log.LOGGER(
            f"{log_prefix} could not stat runtime rootfs {rootfs_path} ({e}); "
            "leaving disk_space unresolved."
        )
        return 0
