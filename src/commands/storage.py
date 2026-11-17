import os
import shutil
from typing import List, Set

from src.utils import partial_service
from src.utils.block_tree import block_tree
from src.utils.config import ConfigManager

env_manager = ConfigManager()

REGISTRY = env_manager.get("REGISTRY")
BLOCKDIR = env_manager.get("BLOCKDIR")


def _live_service_ids() -> Set[str]:
    """The services with a local instance that is not dead.

    A node that revives an instance builds it again from the filesystem of its service.
    If the database cannot be read, every service counts as live: nothing is dropped.
    """
    try:
        from src.database.sql_connection import SQLConnection

        return set(SQLConnection().get_live_local_service_ids())
    except Exception as e:
        print(f"Could not read the live instances ({e}). No filesystem is dropped.")
        return set(partial_service.complete_service_ids(REGISTRY))


def drop_filesystems() -> List[str]:
    """Drop the filesystem of each complete service, and mark it as partial.

    Only with ``misc.OMIT_FS``. A service with a live instance stays complete, and so
    does one that has no block (its filesystem is in its specification). The marker is
    written before the first block of a service is removed, so an interrupted run leaves
    a service that the next run finishes. Nothing is removed if a service cannot be read.
    Returns the ids of the services that became partial.
    """
    live = _live_service_ids()
    dropping: List[str] = []
    for service_id in partial_service.complete_service_ids(REGISTRY):
        if service_id in live:
            print(f"Service {service_id[:6]} has a live instance: its filesystem stays.")
            continue
        if partial_service.filesystem_blocks(service_id, REGISTRY, BLOCKDIR):
            dropping.append(service_id)

    # The blocks that a service that stays complete needs are not removed, whoever else
    # names them.
    needed = partial_service.blocks_in_use(exclude=dropping, registry=REGISTRY, blockdir=BLOCKDIR)

    for service_id in dropping:
        partial_service.mark_partial(service_id, REGISTRY)
        for block in partial_service.filesystem_blocks(service_id, REGISTRY, BLOCKDIR):
            if block in needed:
                continue
            block_path = os.path.join(BLOCKDIR, block)
            if os.path.isdir(block_path):
                shutil.rmtree(block_path)
            elif os.path.exists(block_path):
                os.remove(block_path)
            else:
                continue
            print(f"Block {block[:6]} deleted (filesystem of {service_id[:6]})")
    return dropping


# It will delete all unused cache and blocks.
def prune_blocks():
    """Delete the blocks that no service in the registry needs.

    A service needs the blocks that its ``_.json`` names and the blocks that
    those blocks name, at any depth (see ``block_tree``). A service that is one
    file names no blocks. A block can be one file or a multiblock directory.
    A service that the node keeps without its filesystem (``.nofs``) needs none.

    With ``misc.OMIT_FS`` on, it also drops the filesystem of each complete service first
    (see ``drop_filesystems``). With it off, it does not change a service.
    """
    if partial_service.omit_fs_enabled():
        dropped = drop_filesystems()
        if dropped:
            print(f"{len(dropped)} service(s) are partial now.")

    # Take used blocks, at every depth.
    blocks_used: Set[str] = partial_service.blocks_in_use(registry=REGISTRY, blockdir=BLOCKDIR)

    # Delete unused blocks.
    for block in os.listdir(BLOCKDIR):
        if block not in blocks_used:
            block_path = os.path.join(BLOCKDIR, block)
            if os.path.isdir(block_path):
                shutil.rmtree(block_path)
            else:
                os.remove(block_path)
            print(f"Block {block[:6]} deleted")
