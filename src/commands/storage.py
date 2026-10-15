import os
import shutil
from typing import Set

from src.utils.block_tree import block_tree
from src.utils.config import ConfigManager

env_manager = ConfigManager()

REGISTRY = env_manager.get("REGISTRY")
BLOCKDIR = env_manager.get("BLOCKDIR")


# It will delete all unused cache and blocks.
def prune_blocks():
    """Delete the blocks that no service in the registry needs.

    A service needs the blocks that its ``_.json`` names and the blocks that
    those blocks name, at any depth (see ``block_tree``). A service that is one
    file names no blocks. A block can be one file or a multiblock directory.
    """
    blocks_used: Set[str] = set()

    # Take used blocks, at every depth.
    for service in os.listdir(REGISTRY):
        service_dir = os.path.join(REGISTRY, service)
        if os.path.isdir(service_dir):
            blocks_used.update(block_tree(service_dir, BLOCKDIR))

    # Delete unused blocks.
    for block in os.listdir(BLOCKDIR):
        if block not in blocks_used:
            block_path = os.path.join(BLOCKDIR, block)
            if os.path.isdir(block_path):
                shutil.rmtree(block_path)
            else:
                os.remove(block_path)
            print(f"Block {block[:6]} deleted")
