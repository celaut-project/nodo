"""Services that the node keeps without their filesystem (``misc.OMIT_FS``).

A partial service is a registry entry with its specification, its metadata and the
blocks that are not part of the filesystem, and with no filesystem. The empty file
``.nofs`` next to ``_.json`` marks it. A service without this file is complete. The
marker is the only source of the state: every part of the node asks this module.

A partial service cannot run on this node and cannot be sent to a peer or exported. It
can still be priced and delegated, because a delegation sends hashes only.
"""
import os
from typing import Iterable, List, Optional, Set

from src.utils.block_tree import MissingManifestError, block_tree
from src.utils.config import ConfigManager

env_manager = ConfigManager()

REGISTRY = env_manager.get("REGISTRY")
BLOCKDIR = env_manager.get("BLOCKDIR")

NOFS_MARKER = ".nofs"
OMIT_FS_KEY = "OMIT_FS"


class PartialServiceError(Exception):
    """The operation needs the filesystem of a service that this node does not keep."""


def omit_fs_enabled() -> bool:
    """Whether the node keeps no filesystem of the services that it gets (``misc.OMIT_FS``)."""
    return env_manager.get(OMIT_FS_KEY, False) is True


# ``registry`` and ``blockdir`` of the functions below are for a caller that keeps its own
# copy of the setting (a test, a command that is given another registry). They default to
# the ones of the node.

def service_dir(service_id: str, registry: Optional[str] = None) -> str:
    return os.path.join(registry or REGISTRY, service_id)


def marker_path(service_id: str, registry: Optional[str] = None) -> str:
    return os.path.join(service_dir(service_id, registry), NOFS_MARKER)


def is_partial(service_id: Optional[str], registry: Optional[str] = None) -> bool:
    """Whether the registry holds ``service_id`` without its filesystem."""
    return bool(service_id) and os.path.isfile(marker_path(service_id, registry))


def mark_partial(service_id: str, registry: Optional[str] = None) -> None:
    """Write the marker. The caller removes the blocks after this."""
    with open(marker_path(service_id, registry), "wb"):
        pass


def clear_partial(service_id: str, registry: Optional[str] = None) -> None:
    try:
        os.remove(marker_path(service_id, registry))
    except FileNotFoundError:
        pass


def require_complete(service_id: str, action: str) -> None:
    """Raise ``PartialServiceError`` if ``service_id`` is partial. ``action`` says what was asked."""
    if is_partial(service_id):
        raise PartialServiceError(
            f"The service {service_id} is partial: this node does not keep its filesystem, so "
            f"it cannot {action}. Get the complete service with `nodo download`, with "
            f"misc.OMIT_FS off."
        )


def filesystem_blocks(
        service_id: str, registry: Optional[str] = None, blockdir: Optional[str] = None,
) -> List[str]:
    """The blocks of the filesystem of a service: every block that its ``_.json`` names.

    The packer stores the filesystem as one block (and, in the older inline shape, as one
    block for each large file). The service has no other block. A service that is one
    file, or has no ``_.json``, has none.
    """
    directory = service_dir(service_id, registry)
    if not os.path.isdir(directory):
        return []
    return block_tree(directory, blockdir or BLOCKDIR)


def complete_service_ids(registry: Optional[str] = None) -> List[str]:
    """The ids of the services in the registry that are complete and are a directory."""
    registry = registry or REGISTRY
    try:
        names = os.listdir(registry)
    except OSError:
        return []
    return [
        name for name in names
        if os.path.isdir(os.path.join(registry, name)) and not is_partial(name, registry)
    ]


def blocks_in_use(
        exclude: Iterable[str] = (), registry: Optional[str] = None, blockdir: Optional[str] = None,
) -> Set[str]:
    """The blocks that the complete services need, except those of ``exclude``.

    A partial service does not need a block: it does not use its filesystem. Raises
    ``MissingManifestError`` or ``InvalidBlockIdError`` if a service cannot be read, and
    the caller must then remove nothing.
    """
    skipped = set(exclude)
    used: Set[str] = set()
    for service_id in complete_service_ids(registry):
        if service_id in skipped:
            continue
        used.update(block_tree(service_dir(service_id, registry), blockdir or BLOCKDIR))
    return used


__all__ = [
    "MissingManifestError",
    "NOFS_MARKER",
    "PartialServiceError",
    "blocks_in_use",
    "clear_partial",
    "complete_service_ids",
    "filesystem_blocks",
    "is_partial",
    "mark_partial",
    "omit_fs_enabled",
    "require_complete",
]
