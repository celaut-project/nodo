from __future__ import annotations

import os
import stat
import tarfile
from dataclasses import dataclass
from typing import Mapping, MutableMapping, Optional

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
