import os
import stat
import tarfile
import tempfile
import unittest

from src.utils.filesystem_xattrs import (
    DEVICE_IS_BLOCK_KEY,
    DEVICE_MAJOR_KEY,
    DEVICE_MINOR_KEY,
    GID_KEY,
    MODE_KEY,
    MTIME_NS_KEY,
    UID_KEY,
    FilesystemNodeMetadata,
    encode_filesystem_metadata_xattrs,
    implicit_directory_metadata,
    metadata_from_lstat,
    metadata_from_tarinfo,
    parse_filesystem_metadata_xattrs,
)


def _valid_regular_xattrs() -> dict[str, bytes]:
    return {
        MODE_KEY: str(stat.S_IFREG | 0o640).encode("utf-8"),
        UID_KEY: b"1000",
        GID_KEY: b"1001",
        MTIME_NS_KEY: b"1712345678901234567",
        DEVICE_MAJOR_KEY: b"0",
        DEVICE_MINOR_KEY: b"0",
        DEVICE_IS_BLOCK_KEY: b"0",
    }


class FilesystemXattrsTests(unittest.TestCase):
    def test_roundtrip_regular_metadata(self):
        metadata = FilesystemNodeMetadata(
            mode=stat.S_IFREG | 0o755,
            uid=123,
            gid=456,
            mtime_ns=1712000000000000000,
            device_major=0,
            device_minor=0,
            device_is_block=False,
        )
        xattrs: dict[str, bytes] = {}
        encode_filesystem_metadata_xattrs(xattrs, metadata)

        parsed = parse_filesystem_metadata_xattrs(xattrs)
        self.assertEqual(parsed, metadata)

    def test_parse_returns_none_when_contract_is_missing(self):
        self.assertIsNone(parse_filesystem_metadata_xattrs({}))

    def test_parse_rejects_partial_contract(self):
        with self.assertRaisesRegex(ValueError, "partial filesystem metadata xattrs"):
            parse_filesystem_metadata_xattrs({MODE_KEY: b"33188"})

    def test_parse_rejects_invalid_integer_encoding(self):
        xattrs = _valid_regular_xattrs()
        xattrs[UID_KEY] = b"abc"
        with self.assertRaisesRegex(ValueError, "uid is not a valid base-10 integer"):
            parse_filesystem_metadata_xattrs(xattrs)

    def test_parse_rejects_non_device_with_device_numbers(self):
        xattrs = _valid_regular_xattrs()
        xattrs[DEVICE_MAJOR_KEY] = b"8"
        with self.assertRaisesRegex(
            ValueError, "non-device mode must encode device.major=0"
        ):
            parse_filesystem_metadata_xattrs(xattrs)

    def test_parse_rejects_invalid_device_is_block_flag(self):
        xattrs = _valid_regular_xattrs()
        xattrs[DEVICE_IS_BLOCK_KEY] = b"2"
        with self.assertRaisesRegex(ValueError, "device.is_block must be 0 or 1"):
            parse_filesystem_metadata_xattrs(xattrs)

    def test_metadata_from_lstat_regular_file_sets_zero_device_fields(self):
        with tempfile.NamedTemporaryFile() as tmp:
            st = os.lstat(tmp.name)
        metadata = metadata_from_lstat(st)

        self.assertEqual(metadata.mode, st.st_mode)
        self.assertEqual(metadata.uid, st.st_uid)
        self.assertEqual(metadata.gid, st.st_gid)
        self.assertEqual(metadata.mtime_ns, st.st_mtime_ns)
        self.assertEqual(metadata.device_major, 0)
        self.assertEqual(metadata.device_minor, 0)
        self.assertFalse(metadata.device_is_block)


class MetadataFromTarinfoTests(unittest.TestCase):
    """The service hash is fed by this metadata. tarfile.extractall only chowns
    to the tar's own uid/gid when running as root, so an unprivileged extraction
    stamps everything with the packer's own uid/gid instead — reading metadata
    from the TarInfo rather than the extracted tree is what keeps two different
    users packing the same image at the same hash."""

    def _tarinfo(self, **overrides):
        ti = tarfile.TarInfo(name=overrides.pop("name", "bin/bash"))
        ti.type = overrides.pop("type", tarfile.REGTYPE)
        ti.mode = overrides.pop("mode", 0o755)
        ti.uid = overrides.pop("uid", 0)
        ti.gid = overrides.pop("gid", 0)
        ti.mtime = overrides.pop("mtime", 1712000000)
        ti.devmajor = overrides.pop("devmajor", 0)
        ti.devminor = overrides.pop("devminor", 0)
        assert not overrides, f"unknown overrides: {overrides}"
        return ti

    def test_regular_file_takes_uid_gid_from_the_tar_header(self):
        metadata = metadata_from_tarinfo(self._tarinfo(uid=0, gid=0))
        self.assertEqual(metadata.uid, 0)
        self.assertEqual(metadata.gid, 0)
        self.assertEqual(metadata.mode, stat.S_IFREG | 0o755)
        self.assertEqual(metadata.mtime_ns, 1712000000_000000000)
        self.assertFalse(metadata.is_device)

    def test_same_tar_member_is_identical_regardless_of_who_reads_it(self):
        # The whole point: the extracting uid must not leak into the metadata.
        # A TarInfo carries no notion of "who is extracting", so two identical
        # calls always agree — unlike metadata_from_lstat on an unprivileged
        # extraction, whose st_uid/st_gid would follow the caller instead.
        a = metadata_from_tarinfo(self._tarinfo(uid=0, gid=0))
        b = metadata_from_tarinfo(self._tarinfo(uid=0, gid=0))
        self.assertEqual(a, b)

    def test_directory_type(self):
        metadata = metadata_from_tarinfo(
            self._tarinfo(name="bin/", type=tarfile.DIRTYPE, mode=0o755)
        )
        self.assertEqual(metadata.mode, stat.S_IFDIR | 0o755)
        self.assertFalse(metadata.is_device)

    def test_symlink_mtime_is_zeroed(self):
        # Mirrors metadata_from_lstat: tarfile re-stamps a symlink's mtime to
        # wall-clock on every extract, so restoring the tar's own value would
        # not survive a second extraction, let alone a different host.
        metadata = metadata_from_tarinfo(
            self._tarinfo(type=tarfile.SYMTYPE, mtime=1712000000)
        )
        self.assertEqual(metadata.mtime_ns, 0)

    def test_hardlink_is_treated_as_a_regular_file(self):
        # A tar hardlink (LNKTYPE) carries no mode bit of its own for "this is a
        # hardlink" — once extracted it is a regular file, so it hashes as one.
        metadata = metadata_from_tarinfo(self._tarinfo(type=tarfile.LNKTYPE))
        self.assertEqual(metadata.mode, stat.S_IFREG | 0o755)

    def test_char_device_carries_major_minor(self):
        metadata = metadata_from_tarinfo(
            self._tarinfo(type=tarfile.CHRTYPE, devmajor=1, devminor=5)
        )
        self.assertEqual(metadata.device_major, 1)
        self.assertEqual(metadata.device_minor, 5)
        self.assertFalse(metadata.device_is_block)
        self.assertTrue(metadata.is_device)

    def test_block_device_is_flagged_as_block(self):
        metadata = metadata_from_tarinfo(
            self._tarinfo(type=tarfile.BLKTYPE, devmajor=8, devminor=0)
        )
        self.assertTrue(metadata.device_is_block)

    def test_unsupported_entry_type_raises(self):
        with self.assertRaisesRegex(ValueError, "unsupported tar entry type"):
            metadata_from_tarinfo(self._tarinfo(type=tarfile.GNUTYPE_SPARSE))

    def test_roundtrips_through_the_xattr_contract(self):
        metadata = metadata_from_tarinfo(self._tarinfo())
        xattrs: dict[str, bytes] = {}
        encode_filesystem_metadata_xattrs(xattrs, metadata)
        self.assertEqual(parse_filesystem_metadata_xattrs(xattrs), metadata)


class ImplicitDirectoryMetadataTests(unittest.TestCase):
    def test_is_a_directory_with_no_owner(self):
        metadata = implicit_directory_metadata()
        self.assertTrue(stat.S_ISDIR(metadata.mode))
        self.assertEqual(metadata.uid, 0)
        self.assertEqual(metadata.gid, 0)
        self.assertEqual(metadata.mtime_ns, 0)
        self.assertFalse(metadata.is_device)

    def test_is_deterministic(self):
        self.assertEqual(implicit_directory_metadata(), implicit_directory_metadata())


if __name__ == "__main__":
    unittest.main()
