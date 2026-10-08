"""The blocks that a packed service needs, at every depth.

A service in the registry is a multiblock directory: its ``_.json`` lists the
parts in order, and a part that is a list ``[block_id, positions]`` names a
block in the block directory. A block can itself be a multiblock directory with
its own ``_.json`` that names more blocks (bee-rpc writes a layer that holds a
shared block this way). To read the service, bee-rpc needs every one of these
blocks, so whoever copies a service to another place must copy all of them, not
only the blocks that the service's own ``_.json`` names.
"""
import json
import os
import re
import shutil
from typing import List, Set

MANIFEST = "_.json"

# bee-rpc names a block by the hex digest of its content (sha3-256 by default,
# but the hash is configurable, so the length is not fixed).
BLOCK_ID = re.compile(r"[0-9a-f]+")


class InvalidBlockIdError(ValueError):
    """A ``_.json`` names a block id that is not a lowercase hex digest."""


class MissingManifestError(FileNotFoundError):
    """A service directory has no ``_.json``, so its blocks are not known."""


def manifest_block_ids(directory: str) -> List[str]:
    """The block ids that the ``_.json`` of ``directory`` names, in order.

    A directory without ``_.json`` (a block that is one file, or a missing
    block) names no blocks.

    The callers use a block id as a file name in the block directory. Thus an
    id that is not a lowercase hex digest (for example ``../x`` or ``/etc``) is
    not accepted: ``InvalidBlockIdError`` is raised.
    """
    manifest = os.path.join(directory, MANIFEST)
    if not os.path.isfile(manifest):
        return []
    with open(manifest) as f:
        entries = json.load(f)
    block_ids = [e[0] for e in entries if isinstance(e, list) and e and isinstance(e[0], str)]
    for block_id in block_ids:
        if not BLOCK_ID.fullmatch(block_id):
            raise InvalidBlockIdError(
                f"The file '{manifest}' names the block {block_id!r}, which is not "
                f"a lowercase hex digest. The service in the registry is not correct."
            )
    return block_ids


def block_tree(service_dir: str, blocks_dir: str) -> List[str]:
    """Every block that the service in ``service_dir`` needs.

    These are the blocks that its ``_.json`` names and, for each of them that is
    a multiblock directory in ``blocks_dir``, the blocks that its ``_.json``
    names, at any depth. Each id is given once, a block before the blocks that
    it names. A block that is not in ``blocks_dir`` is in the list, but the
    blocks that it would name are not.

    A block without ``_.json`` is one file and names no blocks. But
    ``service_dir`` must have a ``_.json``: a multiblock service without it is
    not complete, and a copy of it would not hold its blocks. If it has no
    ``_.json``, ``MissingManifestError`` is raised.
    """
    if not os.path.isfile(os.path.join(service_dir, MANIFEST)):
        raise MissingManifestError(
            f"The service directory '{service_dir}' has no {MANIFEST}, so its "
            f"blocks are not known. The service in the registry is not complete."
        )
    found: List[str] = []
    seen: Set[str] = set()

    def visit(block_ids: List[str]) -> None:
        for block_id in block_ids:
            if block_id in seen:
                continue
            seen.add(block_id)
            found.append(block_id)
            visit(manifest_block_ids(os.path.join(blocks_dir, block_id)))

    visit(manifest_block_ids(service_dir))
    return found


def copy_block(blocks_dir: str, block_id: str, dest_dir: str) -> None:
    """Copy the block ``block_id`` from ``blocks_dir`` into ``dest_dir``.

    A block is one file or a multiblock directory. The copy has the same name.
    If the block is not in ``blocks_dir``, a warning is printed and nothing is
    copied.
    """
    source = os.path.join(blocks_dir, block_id)
    destination = os.path.join(dest_dir, block_id)
    if not os.path.exists(source):
        print(f"WARNING: The block {block_id} is not in '{blocks_dir}'. It is not copied.")
    elif os.path.isdir(source):
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)
