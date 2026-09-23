from __future__ import annotations

import os
import stat
import tarfile
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, MutableMapping, Optional

MODE_KEY = "mode"
UID_KEY = "uid"
GID_KEY = "gid"
MTIME_NS_KEY = "mtime_ns"
DEVICE_MAJOR_KEY = "device.major"
DEVICE_MINOR_KEY = "device.minor"
DEVICE_IS_BLOCK_KEY = "device.is_block"

FILESYSTEM_METADATA_KEYS = (
    MODE_KEY,
    UID_KEY,
    GID_KEY,
    MTIME_NS_KEY,
    DEVICE_MAJOR_KEY,
    DEVICE_MINOR_KEY,
    DEVICE_IS_BLOCK_KEY,
)

# `Filesystem.xattrs` -- the map on the tree itself, not on one of its entries.
# Only the Filesystem referenced directly by `Container.filesystem` is read: a
# nested one (a subdirectory, reached through `ItemBranch.item.filesystem`) is
# not separately mounted, so nothing there could be honoured.
READ_MODE_KEY = "read_mode"
READ_MODE_RW = "rw"
READ_MODE_RO = "ro"
READ_MODES = (READ_MODE_RW, READ_MODE_RO)


@dataclass(frozen=True)
class FilesystemNodeMetadata:
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    device_major: int
    device_minor: int
    device_is_block: bool

    @property
    def is_device(self) -> bool:
        return stat.S_ISBLK(self.mode) or stat.S_ISCHR(self.mode)


def is_supported_filesystem_entry_mode(mode: int) -> bool:
    return (
        stat.S_ISREG(mode)
        or stat.S_ISDIR(mode)
        or stat.S_ISLNK(mode)
        or stat.S_ISBLK(mode)
        or stat.S_ISCHR(mode)
    )


def describe_mode_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular_file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISBLK(mode):
        return "block_device"
    if stat.S_ISCHR(mode):
        return "char_device"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "unknown"


def metadata_from_lstat(stat_result: os.stat_result) -> FilesystemNodeMetadata:
    mode = int(stat_result.st_mode)
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        device_major = int(os.major(stat_result.st_rdev))
        device_minor = int(os.minor(stat_result.st_rdev))
        device_is_block = bool(stat.S_ISBLK(mode))
    else:
        device_major = 0
        device_minor = 0
        device_is_block = False
    return FilesystemNodeMetadata(
        mode=mode,
        uid=int(stat_result.st_uid),
        gid=int(stat_result.st_gid),
        mtime_ns=(0 if stat.S_ISLNK(mode) else int(stat_result.st_mtime_ns)),  # tarfile re-stamps symlink mtimes to wall-clock on each extract; regular-file mtimes are restored from the tar and stay deterministic
        device_major=device_major,
        device_minor=device_minor,
        device_is_block=device_is_block,
    )


# tarfile.TarInfo.type -> the S_IF* type bits os.stat would report for the
# same entry. LNKTYPE (a tar hardlink) is deliberately mapped like a regular
# file: that is what it becomes once extracted, and the tar header carries no
# separate "this is a hardlink" mode bit of its own.
_TAR_TYPE_TO_S_IFMT = {
    tarfile.REGTYPE: stat.S_IFREG,
    tarfile.AREGTYPE: stat.S_IFREG,
    tarfile.LNKTYPE: stat.S_IFREG,
    tarfile.DIRTYPE: stat.S_IFDIR,
    tarfile.SYMTYPE: stat.S_IFLNK,
    tarfile.CHRTYPE: stat.S_IFCHR,
    tarfile.BLKTYPE: stat.S_IFBLK,
    tarfile.FIFOTYPE: stat.S_IFIFO,
}


def metadata_from_tarinfo(tarinfo: tarfile.TarInfo) -> FilesystemNodeMetadata:
    # `tarfile.extractall` only chowns extracted entries to the uid/gid recorded
    # in the tar when it is running as root; as an unprivileged user every entry
    # comes out owned by the extracting user instead, silently. Since those
    # uid/gid values feed the content-addressed service hash, packing the same
    # image as two different users produced two different ids. The tar header
    # itself is unaffected by who extracts it, so reading metadata from the
    # tarfile.TarInfo (this function) instead of from the extracted tree
    # (metadata_from_lstat) keeps the hash independent of the packer's uid.
    ifmt = _TAR_TYPE_TO_S_IFMT.get(tarinfo.type)
    if ifmt is None:
        raise ValueError(
            f"unsupported tar entry type {tarinfo.type!r} for '{tarinfo.name}'"
        )
    mode = ifmt | (tarinfo.mode & 0o7777)

    if tarinfo.type in (tarfile.CHRTYPE, tarfile.BLKTYPE):
        device_major = int(tarinfo.devmajor)
        device_minor = int(tarinfo.devminor)
        device_is_block = tarinfo.type == tarfile.BLKTYPE
    else:
        device_major = 0
        device_minor = 0
        device_is_block = False

    return FilesystemNodeMetadata(
        mode=mode,
        uid=int(tarinfo.uid),
        gid=int(tarinfo.gid),
        # Symlink mtimes are zeroed for the same reason metadata_from_lstat
        # zeroes them: tarfile re-stamps a symlink's mtime to wall-clock on
        # each extract, so restoring the tar's own value would not survive a
        # second extraction on the same host, let alone a different one.
        mtime_ns=0 if ifmt == stat.S_IFLNK else int(tarinfo.mtime) * 1_000_000_000,
        device_major=device_major,
        device_minor=device_minor,
        device_is_block=device_is_block,
    )


def implicit_directory_metadata() -> FilesystemNodeMetadata:
    # A directory that tarfile materialized on disk only because a deeper entry
    # needed it as a parent, with no member of its own in the tar (some tar
    # writers omit them). Every packer fabricates the same synthetic values for
    # it, so it stays out of the non-determinism metadata_from_tarinfo exists to
    # avoid.
    return FilesystemNodeMetadata(
        mode=stat.S_IFDIR | 0o755,
        uid=0,
        gid=0,
        mtime_ns=0,
        device_major=0,
        device_minor=0,
        device_is_block=False,
    )


def encode_filesystem_metadata_xattrs(
    xattrs: MutableMapping[str, bytes],
    metadata: FilesystemNodeMetadata,
) -> None:
    xattrs[MODE_KEY] = str(metadata.mode).encode("utf-8")
    xattrs[UID_KEY] = str(metadata.uid).encode("utf-8")
    xattrs[GID_KEY] = str(metadata.gid).encode("utf-8")
    xattrs[MTIME_NS_KEY] = str(metadata.mtime_ns).encode("utf-8")
    xattrs[DEVICE_MAJOR_KEY] = str(metadata.device_major).encode("utf-8")
    xattrs[DEVICE_MINOR_KEY] = str(metadata.device_minor).encode("utf-8")
    xattrs[DEVICE_IS_BLOCK_KEY] = (
        b"1" if metadata.device_is_block else b"0"
    )


def parse_filesystem_metadata_xattrs(
    xattrs: Mapping[str, bytes],
) -> Optional[FilesystemNodeMetadata]:
    present_keys = [key for key in FILESYSTEM_METADATA_KEYS if key in xattrs]
    if not present_keys:
        return None

    missing_keys = [key for key in FILESYSTEM_METADATA_KEYS if key not in xattrs]
    if missing_keys:
        raise ValueError(
            "partial filesystem metadata xattrs: missing "
            + ", ".join(sorted(missing_keys))
        )

    mode = _parse_utf8_int(MODE_KEY, xattrs[MODE_KEY])
    uid = _parse_utf8_int(UID_KEY, xattrs[UID_KEY])
    gid = _parse_utf8_int(GID_KEY, xattrs[GID_KEY])
    mtime_ns = _parse_utf8_int(MTIME_NS_KEY, xattrs[MTIME_NS_KEY])
    device_major = _parse_utf8_int(DEVICE_MAJOR_KEY, xattrs[DEVICE_MAJOR_KEY])
    device_minor = _parse_utf8_int(DEVICE_MINOR_KEY, xattrs[DEVICE_MINOR_KEY])
    device_is_block_int = _parse_utf8_int(
        DEVICE_IS_BLOCK_KEY, xattrs[DEVICE_IS_BLOCK_KEY]
    )

    if uid < 0 or gid < 0:
        raise ValueError("uid and gid must be >= 0")
    if device_major < 0 or device_minor < 0:
        raise ValueError("device major/minor must be >= 0")
    if device_is_block_int not in (0, 1):
        raise ValueError("device.is_block must be 0 or 1")

    is_block_mode = stat.S_ISBLK(mode)
    is_char_mode = stat.S_ISCHR(mode)
    is_device_mode = is_block_mode or is_char_mode
    device_is_block = bool(device_is_block_int)

    if is_device_mode:
        if device_is_block != is_block_mode:
            expected = 1 if is_block_mode else 0
            raise ValueError(
                f"device.is_block mismatch for mode {oct(mode)}: "
                f"expected {expected}, got {device_is_block_int}"
            )
    else:
        if device_major != 0 or device_minor != 0 or device_is_block:
            raise ValueError(
                "non-device mode must encode device.major=0, device.minor=0, "
                "device.is_block=0"
            )

    return FilesystemNodeMetadata(
        mode=mode,
        uid=uid,
        gid=gid,
        mtime_ns=mtime_ns,
        device_major=device_major,
        device_minor=device_minor,
        device_is_block=device_is_block,
    )


def read_mode(filesystem: Any) -> str:
    """Whether this service asked for a writable rootfs: ``"rw"`` or ``"ro"``.

    Absent is ``"rw"``, which is what every service packed before this key existed
    declares by saying nothing -- so reading it can never change how an existing
    service is built.

    Anything else is an integrity error rather than a value to fall back on. The
    key is what decides whether the node builds a pre-sized ext4 the guest can
    write to or an immutable image it cannot, and a typo resolved to a default is
    a service silently built the other way round from the one its author meant.

    Read only from the tree's own ``xattrs``; pass the Filesystem that
    ``Container.filesystem`` points at, not one of its subdirectories.
    """
    xattrs = getattr(filesystem, "xattrs", None) or {}
    if READ_MODE_KEY not in xattrs:
        return READ_MODE_RW

    raw = xattrs[READ_MODE_KEY]
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    try:
        value = bytes(raw).decode("utf-8").strip()
    except Exception as e:
        raise ValueError(f"{READ_MODE_KEY} is not valid UTF-8 bytes") from e

    if value not in READ_MODES:
        raise ValueError(
            f"unsupported {READ_MODE_KEY} '{value}': expected one of "
            + ", ".join(READ_MODES)
        )
    return value


def missing_metadata_keys(xattrs: Mapping[str, bytes]) -> List[str]:
    """Which of :data:`FILESYSTEM_METADATA_KEYS` this entry does not carry."""
    return [key for key in FILESYSTEM_METADATA_KEYS if key not in xattrs]


def assert_complete_filesystem_metadata(
    filesystem: Any,
    parent_rel_path: str = "/",
    resolve_nested: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Refuse a tree that does not declare mode/uid/gid/mtime/device for every entry.

    The metadata keys are optional everywhere else: an entry that carries none of
    them falls back to a legacy heuristic that sniffs shebangs and ELF magic to
    guess an executable bit. That heuristic is enough for a writable image, where
    whatever it got wrong can still be fixed from inside the guest, and it is the
    only thing services packed before the metadata contract existed have.

    It is not enough for an image with no writable escape hatch. It cannot restore
    a uid or a gid -- it does not read them -- and it cannot produce a device node
    at all, so a tree relying on it would be mounted read-only with ownership and
    device nodes it never declared and now cannot repair. So for ``read_mode=ro``
    the keys stop being optional, and a tree missing any of them is refused at
    build time with the path that is missing them, rather than booted wrong.

    Recursive: the gate is about what ends up in the image, and a subdirectory's
    entries end up in it just as much as the root's.

    ``ItemBranch.item.filesystem`` is a ``bytes`` field -- either the literal
    serialized subtree or a pointer to a block holding one, the packer's own
    choice per directory (see ``recursive_parsing`` and
    ``src.utils.container_filesystem``). Resolving that duality means the block
    registry, which this module stays free of on purpose so it keeps testing on
    a bare checkout; ``resolve_nested`` is how a caller that does have it hands
    over the resolved subtree for a directory branch. Left at its default, a
    ``bytes`` field simply has no ``branch`` attribute and the walk stops there
    without recursing -- correct only for a tree with no directory big enough to
    be blocked, which is every tree in this module's own tests.
    """
    if resolve_nested is not None:
        resolve = resolve_nested
    else:
        # Dependency-light default: parses a literal inline subtree (the only
        # shape any tree built directly, with no block registry involved, can
        # be in), by instantiating the same message class this level's own
        # `filesystem` already is -- the type is self-referential, so that is
        # always the right class, with no proto import needed here to name it.
        filesystem_cls = type(filesystem)

        def resolve(branch: Any) -> Any:
            nested = filesystem_cls()
            if branch.filesystem:
                nested.ParseFromString(branch.filesystem)
            return nested

    for branch in getattr(filesystem, "branch", []) or []:
        name = getattr(branch, "name", "") or ""
        rel_path = (
            f"{parent_rel_path.rstrip('/')}/{name}" if name else parent_rel_path
        )

        missing = missing_metadata_keys(getattr(branch, "xattrs", None) or {})
        if missing:
            raise ValueError(
                f"incomplete filesystem metadata at '{rel_path}': missing "
                + ", ".join(sorted(missing))
                + f". A {READ_MODE_KEY}={READ_MODE_RO} service must declare "
                + ", ".join(FILESYSTEM_METADATA_KEYS)
                + " on every entry: the image it is built into cannot be corrected "
                "from inside the guest."
            )

        nested = None
        try:
            if branch.HasField("filesystem"):
                nested = resolve(branch)
        except (AttributeError, ValueError):
            nested = None
        if nested is not None:
            assert_complete_filesystem_metadata(
                nested, parent_rel_path=rel_path, resolve_nested=resolve_nested
            )


def _parse_utf8_int(key: str, value: bytes) -> int:
    if isinstance(value, str):
        value = value.encode("utf-8")
    try:
        text = bytes(value).decode("utf-8")
    except Exception as e:
        raise ValueError(f"{key} is not valid UTF-8 bytes") from e

    stripped = text.strip()
    if not stripped:
        raise ValueError(f"{key} cannot be empty")

    try:
        return int(stripped, 10)
    except ValueError as e:
        raise ValueError(f"{key} is not a valid base-10 integer") from e
