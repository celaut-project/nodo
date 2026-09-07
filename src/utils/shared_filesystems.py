"""Shared filesystems as a parent -> child capability.

A shared directory is a **communication channel**. If an instance that is not a
child of the exporter could mount it, two instances the Networks model keeps in
separate communication domains would be able to exchange data over disk: the
isolation `Service.Network` provides only means something if share gating is at
least as strict. Nothing cryptographic enforces that -- only the node can -- so
these rules are a node-conformance property, verifiable and punishable through
reputation.

The invariant, normatively:

* ``shared`` belongs only to the **creator** of the directory: the instance whose
  own image contains it. There is no re-export and no transfer.
* ``guest`` can only ever be exercised by that instance's **direct children**.
* A service that is not a child of the exporter can never attach to the
  directory. **A node that allows it is malicious.**

Sharing is expressed through reserved xattrs on directories in
``Container.Filesystem.ItemBranch.xattrs`` -- an execution-environment concern,
never a ``Service.Network``:

* ``shared=true`` — exported to the child instances this instance launches.
* ``guest=true``  — inherited from the parent instance that launched this one.
* ``access=ro|rw`` — requested access mode for the mount (defaults to ``rw``).
* ``share_tag=<tag>`` — logical name of the share, used instead of the path.
* ``share_env=<ENV_VAR>`` — names an environment variable whose *value*
  discriminates which concrete share this is.

``guest`` is an **execution precondition, not an option**: declaring that a
directory is inherited means the service cannot run unless an instance exists
that created that directory and exported it to this one. There is no
"``required``" xattr because it is always required, and no partial start -- from
inside a guest, a datanode writing to its local disk is indistinguishable from
one whose share came up empty. The launch fails instead, before anything is
spent; see ``src/manager/shares.py``.

The last two xattrs mirror ``Service.Network``: ``share_tag`` plays the part of
``Network.tags`` (the logical domain) and ``share_env`` that of
``Network.environment_variable``. A share is identified by::

    name          = share_tag        if declared, else the exported path
    discriminator = env[share_env]   if share_env is declared, else ""
    share_id      = H(parent_instance_id, name, share_env, discriminator)

The *name of the variable* is part of the identity, not only its value: that is
what makes the match symmetric, the way ``peer_env_matches`` requires both sides
to agree. Without it a child that declares no ``share_env`` would derive the same
id as a parent that declares one but was launched with no value for it, skipping
the discriminator altogether.

Naming a share independently of where it is mounted is what lets a child mount
the parent's export wherever suits it, and what keeps the concrete dataset out of
the service specification: one content-addressed service id serves a different
dataset just by being launched with a different value for its ``share_env``
variable, with no repack.

A share is **not durable**, and the tag does not make it so: ``parent_instance_id``
stays in the formula, so a share lives exactly as long as the incarnation of the
parent that created it. If the exporter restarts it gets a new instance id, hence
a new share, and the previous directory is removed with the instance that owned
it. Persistence across restarts is not what this mechanism provides.

VirtioFS is one runtime implementation of the materialization (see
``src/virtualizers/microvm/shares.py``); the service specification stays
completely independent of it.

This module is intentionally free of DB / RPC / virtualizer dependencies so the
model stays pure and unit-testable. Everything here is decidable from a spec
alone, which is what makes it validatable at pack time; whether an exporter
actually exists is not, and lives in ``src/manager/shares.py``.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional

from protos import celaut_pb2 as celaut
from src.utils.container_filesystem import load_container_filesystem

# Reserved sharing xattr keys (distinct from the POSIX metadata keys in
# src/utils/filesystem_xattrs.py).
SHARED_XATTR_KEY = "shared"
GUEST_XATTR_KEY = "guest"
ACCESS_XATTR_KEY = "access"
SHARE_TAG_XATTR_KEY = "share_tag"
SHARE_ENV_XATTR_KEY = "share_env"

ACCESS_RO = "ro"
ACCESS_RW = "rw"
_VALID_ACCESS = (ACCESS_RO, ACCESS_RW)

_TRUE_VALUES = frozenset({b"1", b"true", b"yes", b"on"})
_FALSE_VALUES = frozenset({b"", b"0", b"false", b"no", b"off"})

# A tag is matched literally, so what it may contain is pinned down rather than
# left to whoever writes the xattr: surrounding whitespace is stripped, the match
# is case-sensitive, and the charset excludes anything that could make two tags
# look alike (no whitespace inside, no control characters, no separators). An
# env var name is a POSIX one.
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:+-]*$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SharedDir:
    """A directory a service either exports to, or imports from, its parent."""
    path: str        # absolute guest path of the directory (e.g. /mnt/photos)
    shared: bool     # exported to children (shared=true)
    guest: bool      # inherited from parent (guest=true)
    access: str      # "ro" | "rw"
    tag: str = ""    # share_tag: logical name of the share, "" => use the path
    env: str = ""    # share_env: env var discriminating the concrete share

    @property
    def readonly(self) -> bool:
        return self.access == ACCESS_RO

    @property
    def share_name(self) -> str:
        """The share's logical name: its tag when it declares one, else the path
        it is mounted at."""
        return self.tag or self.path


@dataclass(frozen=True)
class ShareRef:
    """One side of a share: its id, plus the parts that name it in the clear.

    The id alone cannot explain a refusal -- it is a hash, so all it can say is
    that X is not in {Y, Z}. Two failures with opposite diagnoses collapse into
    that same non-membership: the parent not exporting this name at all is a
    *composition* error (incompatible services, or launched from the wrong
    parent), while exporting it under a different discriminator is a
    *configuration* error (a typo in the variable, or one left undefined). So the
    name, the variable and its value travel alongside the id.
    """
    share_id: str
    name: str            # share_tag, or the mount path when none is declared
    env: str             # name of the share_env variable, "" if none
    discriminator: str   # value of that variable, "" if none or unset
    path: str            # where this side mounts it
    readonly: bool       # how this side mounts it

    def describe(self) -> str:
        env_part = f", {self.env}={self.discriminator or '<unset>'}" if self.env else ""
        return f"'{self.name}'{env_part}"


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


def _decode_name(path: str, key: str, xattrs: Mapping[str, bytes], pattern) -> str:
    """Read an identifier-valued xattr (``share_tag`` / ``share_env``)."""
    raw = xattrs.get(key)
    if raw is None:
        return ""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    value = bytes(raw).strip().decode("utf-8", "replace")
    if not value:
        raise ValueError(f"xattr '{key}' on '{path}' is declared but empty")
    if not pattern.match(value):
        raise ValueError(
            f"xattr '{key}' on '{path}' is '{value}', which is not a valid "
            f"{key} (expected {pattern.pattern})"
        )
    return value


def declaration_from_xattrs(path: str, xattrs: Mapping[str, bytes]) -> "SharedDir | None":
    """Build a :class:`SharedDir` for ``path`` if its xattrs opt into sharing.

    Returns ``None`` when the directory declares neither ``shared`` nor ``guest``.
    Raises ``ValueError`` on a malformed / contradictory declaration.
    """
    has_shared = SHARED_XATTR_KEY in xattrs and _decode_bool(SHARED_XATTR_KEY, xattrs[SHARED_XATTR_KEY])
    has_guest = GUEST_XATTR_KEY in xattrs and _decode_bool(GUEST_XATTR_KEY, xattrs[GUEST_XATTR_KEY])
    tag = _decode_name(path, SHARE_TAG_XATTR_KEY, xattrs, _TAG_PATTERN)
    env = _decode_name(path, SHARE_ENV_XATTR_KEY, xattrs, _ENV_NAME_PATTERN)
    if not has_shared and not has_guest:
        # A tag or an env var names a share that is neither exported nor
        # imported: nothing would ever be materialized, so it is a typo, not a
        # declaration.
        if tag or env:
            raise ValueError(
                f"directory '{path}' declares {SHARE_TAG_XATTR_KEY}/{SHARE_ENV_XATTR_KEY} "
                f"without '{SHARED_XATTR_KEY}' or '{GUEST_XATTR_KEY}'"
            )
        return None
    if has_shared and has_guest:
        raise ValueError(
            f"directory '{path}' cannot be both shared (export) and guest (import)"
        )
    # The path of a share is the one thing here a *remote* spec gets to point at
    # something outside itself: the node reads the exporter's subtree off the
    # image to seed the share with it. A protobuf carries whatever the sending
    # peer put in it, `..` included, so a declared path that is not already its
    # own normal form is refused rather than resolved.
    if path != posixpath.normpath(path):
        raise ValueError(
            f"share path '{path}' is not normalized; a shared/guest directory "
            f"cannot be named through '.', '..' or an empty component"
        )
    return SharedDir(
        path=path,
        shared=has_shared,
        guest=has_guest,
        access=_decode_access(xattrs),
        tag=tag,
        env=env,
    )


def _join(parent: str, name: str) -> str:
    if not parent.endswith("/"):
        parent += "/"
    return parent + name


def _walk(
    fs: celaut.Service.Container.Filesystem,
    parent_path: str,
    enclosing: Optional[SharedDir],
) -> List[SharedDir]:
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
        if decl is not None and enclosing is not None:
            # Nesting is how a re-export would be smuggled in: a `shared` inside
            # a `guest` subtree hands a grandchild the grandparent's share, which
            # the invariant forbids outright. Two overlapping declarations are
            # also two mounts covering one path, with the winner decided by the
            # order of the mount plan -- undefined, not a policy.
            raise ValueError(
                f"share declaration on '{path}' is nested inside the one on "
                f"'{enclosing.path}'; a shared/guest directory cannot contain "
                f"another, and an inherited directory can never be re-exported"
            )
        if decl is not None:
            out.append(decl)
        if is_dir:
            out.extend(_walk(branch.filesystem, path, decl or enclosing))
    return out


def _reject_duplicate_names(declarations: List[SharedDir]) -> None:
    """One name per side: two directories cannot resolve to a single share.

    A filesystem tree cannot hold one path twice, so path-named shares never
    collide; a ``share_tag`` can be repeated. Two exports sharing a name resolve
    to one share id, hence one virtio-fs tag emitted as two devices and two mount
    points onto one host directory.
    """
    for side, group in (
        ("shared", [d for d in declarations if d.shared]),
        ("guest", [d for d in declarations if d.guest]),
    ):
        seen: Dict[str, str] = {}
        for d in group:
            clash = seen.get(d.share_name)
            if clash is not None:
                raise ValueError(
                    f"'{clash}' and '{d.path}' are both {side} under the name "
                    f"'{d.share_name}'; give them different {SHARE_TAG_XATTR_KEY} values"
                )
            seen[d.share_name] = d.path


def declarations_for_service(service: celaut.Service) -> List[SharedDir]:
    """All shared/guest directory declarations in a service's container fs.

    Raises ``ValueError`` on anything a spec alone can be judged on: a malformed
    xattr, a non-directory, a nested declaration, or two of them resolving to one
    share.
    """
    declarations = _walk(load_container_filesystem(service), "/", None)
    _reject_duplicate_names(declarations)
    return declarations


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


def share_discriminator(
    declaration: SharedDir,
    env_values: Optional[Mapping[str, bytes]] = None,
) -> str:
    """Value that tells apart two shares of the same logical name.

    Empty when the declaration names no ``share_env``, and equally empty when it
    names one the instance was launched without: two instances missing the same
    variable are in the same (unnamed) domain, exactly as two that set it to the
    same value are. That does not weaken the match, because the *name* of the
    variable is hashed too: only a side declaring the same ``share_env`` can
    reach the same share at all.
    """
    if not declaration.env:
        return ""
    raw = (env_values or {}).get(declaration.env)
    if raw is None:
        return ""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return bytes(raw).decode("utf-8", "replace")


def share_id(
    parent_instance_id: str, name: str, env: str = "", discriminator: str = ""
) -> str:
    """Stable content id of a shared directory.

    ``name`` is the declaration's ``share_tag`` or, absent one, the exported
    path; ``env`` is the name of its ``share_env`` variable and ``discriminator``
    that variable's value. Both sides of a share derive the id from the *same*
    parent instance id -- the exporter's own, the importer's ``father_id`` -- so a
    child can never address a share belonging to a different parent, whatever it
    names it.

    The fields are length-prefixed rather than joined by a separator. Two of them
    carry text the spec chose and one an environment value, so with a separator
    ``name="a\\0b"`` and ``name="a", discriminator="b"`` would hash alike and a
    child could reach a share of its parent's it was never granted by putting the
    separator in its own tag. Length-prefixing makes the encoding injective, so
    no validation has to stand in for it.
    """
    if not parent_instance_id:
        raise ValueError("parent_instance_id is required to identify a share")
    material = b"".join(
        len(field).to_bytes(4, "big") + field
        for field in (
            parent_instance_id.encode("utf-8"),
            name.encode("utf-8"),
            env.encode("utf-8"),
            discriminator.encode("utf-8"),
        )
    )
    return hashlib.sha256(material).hexdigest()


def share_ref(
    parent_instance_id: str,
    declaration: SharedDir,
    env_values: Optional[Mapping[str, bytes]] = None,
) -> ShareRef:
    """Resolve one declaration into a :class:`ShareRef`, as seen by an instance
    whose environment is ``env_values``."""
    discriminator = share_discriminator(declaration, env_values)
    return ShareRef(
        share_id=share_id(
            parent_instance_id, declaration.share_name, declaration.env, discriminator
        ),
        name=declaration.share_name,
        env=declaration.env,
        discriminator=discriminator,
        path=declaration.path,
        readonly=declaration.readonly,
    )


def exported_refs(
    service: celaut.Service,
    instance_id: str,
    env_values: Optional[Mapping[str, bytes]] = None,
) -> List[ShareRef]:
    """The shares this instance exports to the children it launches."""
    return [share_ref(instance_id, d, env_values) for d in exported_dirs(service)]


def guest_refs(
    service: celaut.Service,
    father_id: str,
    env_values: Optional[Mapping[str, bytes]] = None,
) -> List[ShareRef]:
    """The shares this instance inherits from the parent that launched it."""
    if not father_id:
        return []
    return [share_ref(father_id, d, env_values) for d in guest_dirs(service)]
