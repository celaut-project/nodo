import os
import shutil

from src.commands.__by_tag import get_id
from src.commands.inspect_service import format_size
from src.utils.config import ConfigManager

env_manager = ConfigManager()

METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")


def _running_instances_of(service_id: str) -> list:
    """Ids of the local instances launched from ``service_id``.

    Only to be able to say so. Removing a service does not stop what is already
    running, and the bundle is not read again once an instance holds its own copy
    of the image, so nothing here blocks the removal.
    """
    try:
        from src.database.sql_connection import SQLConnection

        sc = SQLConnection()
        return [
            instance_id
            for instance_id in sc.get_all_internal_containers_ids()
            if sc.get_service_id_by_container_id(id=instance_id) == service_id
        ]
    except Exception as e:
        print(f"Warning: could not check for running instances of {service_id}: {e}")
        return []


def _remove_path(path: str, description: str) -> None:
    if os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)
    else:
        print(f"Warning: {description} {path} does not exist.")


def remove(service: str):
    service_id = get_id(service)

    # Check if script is run as root
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return

    # `get_id` answers "" for a name it cannot resolve, and every path built below
    # from "" is its own parent directory -- so an unknown name used to delete the
    # whole registry, and would now take the whole build cache with it. Nothing is
    # removed for a service the node does not know.
    if not service_id:
        print(f"No service on this node matches '{service}'. Nothing was removed.")
        return

    running = _running_instances_of(service_id)

    _remove_path(os.path.join(REGISTRY, service_id), "registry entry")
    _remove_path(os.path.join(METADATA_REGISTRY, service_id), "metadata entry")

    # The registry entry is the service's definition; the bundle is the multi-GB
    # rootfs image built from it. Removing only the first left the second in the
    # cache forever -- the disk came back only by deleting __cache__ by hand.
    try:
        from src.virtualizers.interface import remove_built_service

        freed = remove_built_service(service_hash=service_id)
        if freed:
            print(f"Freed {format_size(freed)} of built image for {service_id}.")
        else:
            print(f"No built image was cached for {service_id}.")
    except Exception as e:
        print(f"Warning: could not remove the built image of {service_id}: {e}")

    if running:
        print(
            f"Note: {len(running)} instance(s) of this service are still running and keep "
            "their own copy of the image; launching this service again will rebuild it."
        )

    print(f'Service {service_id} removed from the node.')
