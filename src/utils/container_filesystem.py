"""Reading a service's container filesystem, whichever shape it is stored in.

``Service.Container.filesystem`` is a ``bytes`` field holding a serialized
``Filesystem``. It comes in two shapes:

* **inline** — the serialized filesystem itself, block pointers for individual
  large files embedded in it. Every service packed before the filesystem became
  a block of its own has this shape, and it stays readable forever.
* **a pointer** — a short ``Buffer.Block`` (tens of bytes) standing for the
  whole filesystem, stored as one multiblock block in the block registry.

The second shape exists because the first put the entire rootfs in the way of
every reader of the spec. A spec is read to answer questions that have nothing
to do with the filesystem -- which ports does this service expose, what does it
cost, does it need a parent-exported directory -- and inline, each of those cost
a full copy of every file the image holds. A pointer costs tens of bytes, and
only the one caller that genuinely needs the tree pays for it, here.

Both shapes expand to exactly the same bytes, so a service's content-addressed
id does not depend on which one it is stored in.

``ItemBranch.item.filesystem`` (a subdirectory) is a ``bytes`` field with the
same duality, one level down, once it is large enough for the packer to store
it as a block of its own rather than embed it in its parent -- see
``resolve_filesystem_bytes`` and ``load_branch_filesystem``.
"""
import os
import warnings
from typing import Optional, Sequence, Tuple

from bee_rpc import buffer_pb2
from bee_rpc.client import get_hash_from_block
from bee_rpc.reader import block_exists, read_block
from bee_rpc.utils import Enviroment, WITHOUT_BLOCK_POINTERS_FILE_NAME, HashTypeError, \
    block_id_from_pointer, hash_types_for_packing, inherit_hash_types, resolve_hash_types
from google.protobuf.message import DecodeError

from protos import celaut_pb2 as celaut
from src.utils import logger as log

# A pointer holds one hash: its type and value, tens of bytes. Generous enough
# for several hashes, far below anything an inline filesystem could be.
_LARGEST_PLAUSIBLE_POINTER = 512


def _as_pointer(raw: bytes) -> Optional[buffer_pb2.Buffer.Block]:
    """The ``Buffer.Block`` these bytes are, if they could plausibly be one.

    The size check is not an optimisation. An inline filesystem is the entire
    image, and handing all of it to ``ParseFromString`` on every call -- this
    runs twice per launch through ``declarations_for_service`` -- is a lot of
    work to conclude it was never a pointer. One hash is a few dozen bytes;
    nothing remotely near this cap is one.
    """
    if not raw or len(raw) > _LARGEST_PLAUSIBLE_POINTER:
        return None

    block = buffer_pb2.Buffer.Block()
    try:
        # Parsing bytes that were never a Block is the expected case here, and
        # upb warns about the ones that half-parse. Nothing to report: a wrong
        # guess is answered with None.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            block.ParseFromString(raw)
    except (DecodeError, UnicodeDecodeError):
        return None

    return block if block.hashes else None


def filesystem_block_id(
        raw: bytes,
        inherited: Optional[Sequence[bytes]] = None
) -> Optional[str]:
    """The block this filesystem field points at, or ``None`` if it is inline.

    Settled by the block registry rather than by the bytes: a pointer names a
    hash we actually hold. An inline filesystem would have to both parse as a
    ``Buffer.Block`` *and* name a stored block to be mistaken for one, and what
    it would name is a hash of the whole rootfs.

    ``inherited`` is the hash-type context these bytes sit in. ``None`` is the
    spec's own field, which is the top of a stored tree and carries its types.
    """
    block = _as_pointer(raw)
    if block is None:
        return None

    block_id = block_id_from_pointer(block=block, inherited=inherited)
    if not block_id:
        # Artefacts written before the top of a tree was required to carry its
        # types: a single hash of the empty type, which meant "whatever this node
        # addresses blocks with".
        block_id = get_hash_from_block(block=block, internal_block=True)
    if not block_id:
        return None

    return block_id if block_exists(block_id=block_id) else None


def filesystem_hash_types(service: celaut.Service) -> Optional[Tuple[bytes, ...]]:
    """The hash-type context the pointers *inside* the filesystem tree sit in.

    A filesystem stored as a block of its own is one level down, so the per-file
    pointers in it may leave their hash types out and take them from the pointer
    that named the filesystem -- which is what makes the tree cheap to store when
    it holds thousands of files. Reading those pointers back means knowing what
    they inherit, and that is this.

    ``None`` when the filesystem is inline: it is then part of the spec itself,
    the top of the tree, where a pointer has to carry its own types.
    """
    raw = service.container.filesystem
    if filesystem_block_id(raw) is None:
        return None

    block = _as_pointer(raw)
    try:
        return inherit_hash_types(resolve_hash_types(block), None)
    except HashTypeError:
        # A pointer from before the top was required to carry its types. The empty
        # type meant "whatever this node addresses blocks with", so that is the
        # context it sets for everything below it.
        return hash_types_for_packing()


def resolve_filesystem_bytes(
        raw: bytes,
        inherited: Optional[Sequence[bytes]] = None,
) -> bytes:
    """The serialized ``Filesystem`` these bytes decode to, reading its block if it is one.

    The one operation both ``Container.filesystem`` (the root) and
    ``ItemBranch.item.filesystem`` (a subdirectory, once large enough to be a
    block of its own -- see the packer's ``recursive_parsing``) are read through:
    both are ``bytes`` fields with the same inline-or-pointer duality, so a
    caller walking the tree does not need to know at which depth it is standing.

    ``inherited`` is ``None`` at the root, where the field is the top of the
    stored tree and its pointer, if it is one, carries its own hash types. A
    subdirectory's pointer omits them, the same way a per-file pointer does, and
    relies on the context ``filesystem_hash_types`` resolves for the tree as a
    whole -- pass that here.

    Never the block's *expansion*: see ``_filesystem_block_bytes``.
    """
    if not raw:
        return raw

    block_id = filesystem_block_id(raw, inherited=inherited)
    if block_id is None:
        return raw

    log.LOGGER(f"Reading filesystem block {block_id}.")
    return _filesystem_block_bytes(block_id)


def load_container_filesystem(service: celaut.Service) -> celaut.Service.Container.Filesystem:
    """The service's filesystem tree, fetched from its block when it is one.

    Reads the whole inlined part of the image into memory either way, so call it
    only where the tree itself is needed -- not to answer a question about the
    spec beside it.
    """
    filesystem = celaut.Service.Container.Filesystem()
    resolved = resolve_filesystem_bytes(service.container.filesystem)
    if resolved:
        filesystem.ParseFromString(resolved)
    return filesystem


def load_branch_filesystem(
        branch: celaut.Service.Container.Filesystem.ItemBranch,
        inherited: Optional[Sequence[bytes]],
) -> celaut.Service.Container.Filesystem:
    """A directory ``ItemBranch``'s subtree, fetched from its block when it is one.

    The branch-level counterpart of ``load_container_filesystem``. Call only
    where ``branch.HasField("filesystem")`` -- it does not check, since every
    caller already has to branch on that to tell a directory from a file or a
    link.

    ``inherited`` must be the hash-type context resolved for the tree this
    branch sits in (``filesystem_hash_types``), not ``None``: unlike the root
    field, a subdirectory's own pointer, when it is one, omits its type.
    """
    filesystem = celaut.Service.Container.Filesystem()
    resolved = resolve_filesystem_bytes(branch.filesystem, inherited=inherited)
    if resolved:
        filesystem.ParseFromString(resolved)
    return filesystem


def _filesystem_block_bytes(block_id: str) -> bytes:
    """The filesystem message as stored, with the per-file pointers still in it.

    Deliberately *not* the block's expansion. Expanding substitutes every
    sub-block's content back inline, which would pull the large files into
    memory as well -- exactly what storing them out of line avoids. The
    consumer of this tree (`ch/build.py`'s `_write_item`) resolves a pointer by
    streaming that block straight to its place in the rootfs, so it wants the
    pointers, and only the small inlined files are ever held.

    A multiblock directory keeps that form in its `wbp.bin`. A block stored as a
    single file has no sub-blocks to point at, so its content is already it.
    """
    path = os.path.join(Enviroment.block_dir, block_id)
    if os.path.isdir(path):
        with open(os.path.join(path, WITHOUT_BLOCK_POINTERS_FILE_NAME), "rb") as f:
            return f.read()
    return b"".join(read_block(block_id=block_id, ignore_blocks=True))
