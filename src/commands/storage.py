import json
import os
from typing import List

from src.utils.env import REGISTRY, BLOCKDIR


# It will delete all unused cache and blocks.
def prune_blocks():
    blocks_used: List[str] = []

    # Take used blocks..
    for service in os.listdir(REGISTRY):
        _js = json.load(open(f"{REGISTRY}/{service}/_.json"))
        for partition in _js:
            if type(partition) is list:
                blocks_used.append(partition[0])

    # Delete unused blocks.
    for block in os.listdir(BLOCKDIR):
        if block not in blocks_used:
            os.remove(f"{BLOCKDIR}/{block}")
            print(f"Block {block[:6]} deleted")
