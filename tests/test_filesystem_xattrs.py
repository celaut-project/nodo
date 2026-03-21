import os
import stat
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
    metadata_from_lstat,
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


if __name__ == "__main__":
    unittest.main()
