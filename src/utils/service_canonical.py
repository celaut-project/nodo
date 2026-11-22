"""Where the node checks the canonical encoding of a service (docs/CANONICAL_ENCODING.md).

``canonical_proto`` says if some bytes are the canonical encoding. This module says what the
node does with the answer and where it reads the bytes from:

* the packer checks the specification and the filesystem that it made, before it stores them;
* a node that gets a service (``StartService``, ``GetService``, ``nodo import``) checks the
  specification, and with ``StartService`` the id too (``verify_received_id``).

What is checked is the specification as stored: ``wbp.bin`` of a multiblock directory, the
message with the pointers of the blocks and not their content. The content of a block is
bytes that the hash covers. The nodes that hold the same id hold the same bytes, so a service
that is not canonical is refused by each of them, and not by the first one that sees it.

``misc.CANONICAL_SERVICE`` says what happens when the check fails: ``refuse`` (the default),
``warn`` (log it and go on), ``off`` (do not check). It is for the nodes that still hold
services that were packed before the rules.
"""
import contextlib
import mmap
import os
from typing import Iterator, Optional, Union

from bee_rpc.block_driver import WITHOUT_BLOCK_POINTERS_FILE_NAME
from bee_rpc.reader import read_block

from protos import celaut_pb2
from src.utils import logger as log
from src.utils.canonical_proto import NotCanonical, check
from src.utils.config import ConfigManager
from src.utils.hashing import get_configured_hash_spec, hash_stream
from src.utils.service_content import read_service_content

env_manager = ConfigManager()

SETTING = "misc.CANONICAL_SERVICE"
REFUSE, WARN, OFF = "refuse", "warn", "off"


class ServiceNotCanonical(Exception):
    """A service that the node refuses because its bytes are not the canonical encoding."""


class ServiceIdMismatch(Exception):
    """A service whose content does not hash to the id that came with it."""


def policy() -> str:
    """``misc.CANONICAL_SERVICE``. A value that is not one of the three is ``refuse``: a
    typo does not turn the check off."""
    value = str(env_manager.get(SETTING, REFUSE) or REFUSE).strip().lower()
    return value if value in (REFUSE, WARN, OFF) else REFUSE


def _enforce(data, descriptor, what: str) -> None:
    mode = policy()
    if mode == OFF:
        return
    try:
        check(descriptor, data)
    except NotCanonical as e:
        message = (f"{what} is not the canonical encoding ({e}). The id of a service is the "
                   f"hash of its bytes, so the same service would have other ids. "
                   f"See docs/CANONICAL_ENCODING.md.")
        if mode == WARN:
            log.LOGGER(f"{message} Kept because {SETTING} is {WARN}.")
            return
        raise ServiceNotCanonical(f"{message} Set {SETTING} to {WARN} to keep it anyway.") from e


def spec_file(service_path: Union[str, os.PathLike]) -> str:
    """The file that holds the specification of a service: ``wbp.bin`` of a multiblock
    directory, or the file itself."""
    path = str(service_path)
    return os.path.join(path, WITHOUT_BLOCK_POINTERS_FILE_NAME) if os.path.isdir(path) else path


@contextlib.contextmanager
def _mapped(path: str) -> Iterator[Union[bytes, mmap.mmap]]:
    """The bytes of a file with no copy: a specification of a service that was packed with
    its filesystem inline can be as large as the image."""
    with open(path, "rb") as f:
        if os.fstat(f.fileno()).st_size == 0:
            yield b""
            return
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            yield mapped


def enforce_spec(data, what: str = "The specification of the service") -> None:
    """Check the bytes of a ``Service`` (its filesystem as a pointer, or as bytes)."""
    _enforce(data, celaut_pb2.Service.DESCRIPTOR, what)


def enforce_spec_path(service_path: Union[str, os.PathLike], what: Optional[str] = None) -> None:
    """Check the specification of the service stored at ``service_path``."""
    if policy() == OFF:
        return
    try:
        with _mapped(spec_file(service_path)) as data:
            enforce_spec(data, what or f"The specification of the service at {service_path}")
    except OSError as e:
        raise ServiceNotCanonical(
            f"The specification of the service cannot be read to check it ({e}).") from e


def enforce_filesystem(data, what: str = "The filesystem of the service") -> None:
    _enforce(data, celaut_pb2.Service.Container.Filesystem.DESCRIPTOR, what)


def enforce_filesystem_block(block_id: str, block_dir: str) -> None:
    """Check the filesystem that the packer stored as the block ``block_id``: the message with
    the pointers of the files that are blocks, as ``container_filesystem`` reads it."""
    if policy() == OFF:
        return
    path = os.path.join(block_dir, block_id)
    what = f"The filesystem block {block_id}"
    if os.path.isdir(path):
        with _mapped(os.path.join(path, WITHOUT_BLOCK_POINTERS_FILE_NAME)) as data:
            enforce_filesystem(data, what)
    else:
        enforce_filesystem(b"".join(read_block(block_id=block_id, ignore_blocks=True)), what)


def verify_received_id(service_path: Union[str, os.PathLike], service_hash: str) -> None:
    """Raise :class:`ServiceIdMismatch` unless the content at ``service_path`` hashes to
    ``service_hash`` under the hash of this node. The node stores a service under the id that
    came with it, so a request that carried another content under that id would be kept."""
    computed = hash_stream(read_service_content(service_path),
                           get_configured_hash_spec(env_manager)).hex()
    if computed != service_hash:
        raise ServiceIdMismatch(
            f"The service received hashes to {computed}, and it came with the id "
            f"{service_hash}.")
