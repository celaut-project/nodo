"""Putting the node's data inside the guest's own filesystem, offline.

Every write into a guest image goes through ``debugfs``, never a loop mount:
that is what lets a node build and launch guests without root and without
``CAP_SYS_ADMIN`` (see ``docs/ROOTLESS.md``). Both hypervisors inject the same
three things into the same image the same way -- the serialized
``ConfigurationFile`` at the path the service declared, the resolved entrypoint,
and (when there are shares) the virtiofs mount plan -- so this is one
implementation, not a convention two backends each re-implement.
"""
import posixpath
from pathlib import Path
from typing import List, Optional

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


def guest_config_targets(service: celaut.Service) -> List[str]:
    _ = service
    return list(GUEST_CONFIG_TARGETS)


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
