"""Shared filesystems as parent -> child resource inheritance.

Filesystem sharing is an *execution-environment* concern, not a communication
one, so it is expressed through ``Container.Filesystem.ItemBranch.xattrs`` rather
than through ``Service.Network``. A directory in a service's container filesystem
may carry these reserved xattrs:

* ``shared=true`` — this directory may be shared with the child instances this
  instance launches (an *export*).
* ``guest=true``  — this directory must be inherited from the parent instance
  that launched this one (an *import*).
* ``access=ro|rw`` — requested access mode for the mount (defaults to ``rw``).

Sharing is only ever allowed between a parent instance and the children it
launches. There is no public mechanism for attaching to some other instance's
shared filesystem: the parent launching the child is itself the authorization.
A share is therefore identified by ``(parent_instance_id, export_path)`` — a
child derives exactly the same identity from its own ``father_id`` and the guest
path it declares, so it can only ever inherit a directory its own parent exported
at that path.

VirtioFS is one runtime implementation of the materialization (see
``src/virtualizers/ch/virtiofs.py``); the service specification stays completely
independent of it.

This module is intentionally free of DB / RPC / virtualizer dependencies so the
model stays pure and unit-testable.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Mapping

from protos import celaut_pb2 as celaut
from src.utils.container_filesystem import load_container_filesystem

# Reserved sharing xattr keys (distinct from the POSIX metadata keys in
# src/utils/filesystem_xattrs.py).
SHARED_XATTR_KEY = "shared"
GUEST_XATTR_KEY = "guest"
ACCESS_XATTR_KEY = "access"

ACCESS_RO = "ro"
ACCESS_RW = "rw"
_VALID_ACCESS = (ACCESS_RO, ACCESS_RW)

SHARING_XATTR_KEYS = (SHARED_XATTR_KEY, GUEST_XATTR_KEY, ACCESS_XATTR_KEY)

_TRUE_VALUES = frozenset({b"1", b"true", b"yes", b"on"})
_FALSE_VALUES = frozenset({b"", b"0", b"false", b"no", b"off"})


@dataclass(frozen=True)
class SharedDir:
    """A directory a service either exports to, or imports from, its parent."""
    path: str        # absolute guest path of the directory (e.g. /mnt/photos)
    shared: bool     # exported to children (shared=true)
    guest: bool      # inherited from parent (guest=true)
    access: str      # "ro" | "rw"

    @property
    def readonly(self) -> bool:
        return self.access == ACCESS_RO


def _decode_bool(key: str, value) -> bool:
    if isinstance(value, str):
        value = value.encode("utf-8")
    value = bytes(value).strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"xattr '{key}' must be a boolean (true/false), got {value!r}")


def _decode_access(xattrs: Mapping[str, bytes]) -> str:
    raw = xattrs.get(ACCESS_XATTR_KEY)
    if raw is None:
        return ACCESS_RW  # default to read-write
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    access = bytes(raw).strip().lower().decode("utf-8", "replace")
    if access not in _VALID_ACCESS:
        raise ValueError(
            f"xattr '{ACCESS_XATTR_KEY}' must be one of {_VALID_ACCESS}, got '{access}'"
        )
    return access


def _join(parent: str, name: str) -> str:
    if not parent.endswith("/"):
        parent += "/"
    return parent + name


def declaration_from_xattrs(path: str, xattrs: Mapping[str, bytes]) -> "SharedDir | None":
    """Build a :class:`SharedDir` for ``path`` if its xattrs opt into sharing.

    Returns ``None`` when the directory declares neither ``shared`` nor ``guest``.
    Raises ``ValueError`` on a malformed / contradictory declaration.
    """
    has_shared = SHARED_XATTR_KEY in xattrs and _decode_bool(SHARED_XATTR_KEY, xattrs[SHARED_XATTR_KEY])
    has_guest = GUEST_XATTR_KEY in xattrs and _decode_bool(GUEST_XATTR_KEY, xattrs[GUEST_XATTR_KEY])
    if not has_shared and not has_guest:
        return None
    if has_shared and has_guest:
        raise ValueError(
            f"directory '{path}' cannot be both shared (export) and guest (import)"
        )
    return SharedDir(
        path=path,
        shared=has_shared,
        guest=has_guest,
        access=_decode_access(xattrs),
    )


def _walk(fs: celaut.Service.Container.Filesystem, parent_path: str) -> List[SharedDir]:
    out: List[SharedDir] = []
    for branch in fs.branch:
        path = _join(parent_path, branch.name)
        is_dir = branch.HasField("filesystem")
        decl = declaration_from_xattrs(path, dict(branch.xattrs))
        if decl is not None and not is_dir:
            raise ValueError(
                f"sharing xattrs (shared/guest) are only valid on directories; "
                f"'{path}' is not a directory"
            )
        if decl is not None:
            out.append(decl)
        if is_dir:
            out.extend(_walk(branch.filesystem, path))
    return out


def declarations_for_service(service: celaut.Service) -> List[SharedDir]:
    """All shared/guest directory declarations in a service's container fs."""
    return _walk(load_container_filesystem(service), "/")


def exported_dirs(service: celaut.Service) -> List[SharedDir]:
    """Directories this service exports to its children (shared=true)."""
    return [d for d in declarations_for_service(service) if d.shared]


def guest_dirs(service: celaut.Service) -> List[SharedDir]:
    """Directories this service inherits from its parent (guest=true)."""
    return [d for d in declarations_for_service(service) if d.guest]


def service_requires_parent_colocation(service: celaut.Service) -> bool:
    """True if the service imports any directory from its parent and therefore
    must be scheduled on the same node as that parent."""
    return bool(guest_dirs(service))


def share_id(parent_instance_id: str, export_path: str) -> str:
    """Stable content id of a shared directory.

    Derived only from the parent instance id and the exported path, so a child
    reproduces it from its own ``father_id`` + guest path and can never address a
    share belonging to a different parent.
    """
    if not parent_instance_id:
        raise ValueError("parent_instance_id is required to identify a share")
    material = f"{parent_instance_id}\x00{export_path}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()
