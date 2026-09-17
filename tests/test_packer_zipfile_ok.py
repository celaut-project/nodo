"""Unit tests for the local packer's zip unpacking step (issue #372).

`zipfile_ok()` is the entry point every local pack goes through. It used to be

    os.system('unzip ' + zip + ' -d ' + CACHE + aux_id + '/for_build')
    os.system('rm ' + zip)

which required an `unzip` binary that nodo never declared, ignored its exit
status, and deleted the zip regardless — so a host without `unzip` packed an
empty directory and got told `service.json` was missing, with the evidence gone.

These tests pin the replacement's contract:

  * a real zip is unpacked with the stdlib, through a CACHE path containing a
    space (the old shell-out broke there);
  * the executable bit survives, because `ZipFile.extractall` drops it and a
    service whose entrypoint is `chmod +x` would fail at `RUN ./entrypoint.sh`;
  * a corrupt zip raises **and leaves the zip on disk**;
  * `../evil`, absolute and symlink members are refused;
  * the zip is deleted only after the extraction succeeded;
  * `aux_id` is uuid4 hex.

`ok()` — which drives BuildKit — is mocked throughout; nothing here builds.
"""
import os
import stat
import tempfile
import unittest
import uuid
import zipfile
from unittest import mock

IMPORT_ERROR = None
try:
    # Before anything that builds a ConfigManager at import: the shipped example
    # points STORAGE at /nodo, which only exists on an installed node.
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.packers import zip_with_dockerfile as packer
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    packer = None  # type: ignore[assignment]


def _write_zip(path, entries):
    """Build a zip. `entries` is a list of (name, data, external_attr)."""
    with zipfile.ZipFile(path, "w") as z:
        for name, data, external_attr in entries:
            info = zipfile.ZipInfo(name)
            if external_attr is not None:
                info.external_attr = external_attr
            z.writestr(info, data)


def _regular(mode=0o644):
    return (stat.S_IFREG | mode) << 16


def _symlink(mode=0o777):
    return (stat.S_IFLNK | mode) << 16


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ZipfileOkTests(unittest.TestCase):
    """Everything below runs with CACHE pointed at a temp dir whose path has a
    SPACE in it. The old `os.system('unzip ' + ...)` could not unpack there at
    all; the stdlib does not care, and these tests would catch a regression back
    to string-interpolated shell."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo packer test-")
        self.addCleanup(self._tmp.cleanup)
        # Trailing separator: CACHE is configured that way and the code joins
        # onto it, so the tests exercise the real shape.
        self.cache = os.path.join(self._tmp.name, "a cache dir") + os.sep
        os.makedirs(self.cache, exist_ok=True)
        self.assertIn(" ", self.cache)

        cache_patch = mock.patch.object(packer, "CACHE", self.cache)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

        # ok() shells out to BuildKit. Replace it with a recorder.
        self.ok = mock.Mock(return_value=("serviceid", None, "/dev/null"))
        ok_patch = mock.patch.object(packer, "ok", self.ok)
        ok_patch.start()
        self.addCleanup(ok_patch.stop)

        self.zip_path = os.path.join(self._tmp.name, "a service.zip")

    # -- the extracted path handed to ok() ---------------------------------- #

    def _extracted_dir(self):
        """The `path=` ok() was called with, asserted to exist."""
        self.ok.assert_called_once()
        path = self.ok.call_args.kwargs["path"]
        self.assertTrue(os.path.isdir(path), f"{path} is not a directory")
        return path

    # -- happy path --------------------------------------------------------- #

    def test_a_real_zip_is_extracted(self):
        _write_zip(self.zip_path, [
            ("service.json", b'{"architecture": "linux/amd64"}', _regular()),
            ("Dockerfile", b"FROM scratch\n", _regular()),
            ("src/app.py", b"print('hi')\n", _regular()),
        ])

        packer.zipfile_ok(zip=self.zip_path)

        extracted = self._extracted_dir()
        with open(os.path.join(extracted, "service.json"), "rb") as f:
            self.assertEqual(f.read(), b'{"architecture": "linux/amd64"}')
        with open(os.path.join(extracted, "src", "app.py"), "rb") as f:
            self.assertEqual(f.read(), b"print('hi')\n")

    def test_the_executable_bit_is_preserved(self):
        # ZipFile.extractall drops this. A service whose entrypoint was committed
        # executable must still be executable when BuildKit gets the context.
        _write_zip(self.zip_path, [
            ("service.json", b"{}", _regular()),
            ("entrypoint.sh", b"#!/bin/sh\nexec app\n", _regular(0o755)),
            ("README", b"not executable\n", _regular(0o644)),
        ])

        packer.zipfile_ok(zip=self.zip_path)

        extracted = self._extracted_dir()
        entry = os.path.join(extracted, "entrypoint.sh")
        self.assertTrue(os.access(entry, os.X_OK), "entrypoint lost its +x")
        self.assertEqual(stat.S_IMODE(os.stat(entry).st_mode), 0o755)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(extracted, "README")).st_mode), 0o644
        )

    def test_a_successful_unpack_deletes_the_zip(self):
        _write_zip(self.zip_path, [("service.json", b"{}", _regular())])

        packer.zipfile_ok(zip=self.zip_path)

        self.assertFalse(
            os.path.exists(self.zip_path),
            "the zip should be removed once its contents are on disk",
        )

    def test_aux_id_is_uuid4_hex(self):
        _write_zip(self.zip_path, [("service.json", b"{}", _regular())])

        packer.zipfile_ok(zip=self.zip_path)

        aux_id = self.ok.call_args.kwargs["aux_id"]
        # Parses as a uuid4 and is the hex form — not str(random.random()),
        # which would contain a '.' and fail here.
        self.assertEqual(uuid.UUID(hex=aux_id).version, 4)
        self.assertEqual(aux_id, uuid.UUID(hex=aux_id).hex)
        self.assertNotIn(".", aux_id)
        # And it is the directory actually used.
        self.assertIn(aux_id, self.ok.call_args.kwargs["path"])

    def test_directory_members_are_created_even_when_empty(self):
        _write_zip(self.zip_path, [
            ("service.json", b"{}", _regular()),
            ("emptydir/", b"", (stat.S_IFDIR | 0o755) << 16),
        ])

        packer.zipfile_ok(zip=self.zip_path)

        self.assertTrue(os.path.isdir(os.path.join(self._extracted_dir(), "emptydir")))

    # -- failure paths ------------------------------------------------------ #

    def test_a_corrupt_zip_raises_and_keeps_the_zip(self):
        with open(self.zip_path, "wb") as f:
            f.write(b"this is not a zip file at all")

        with self.assertRaises(packer.ZipExtractionError) as caught:
            packer.zipfile_ok(zip=self.zip_path)

        # The message names the zip, so the operator knows which file to look at.
        self.assertIn(self.zip_path, str(caught.exception))
        self.assertTrue(
            os.path.exists(self.zip_path),
            "a failed unpack must not delete the only copy of the input",
        )
        self.ok.assert_not_called()

    def test_a_corrupt_member_raises_and_keeps_the_zip(self):
        # Valid container, corrupt payload. Reading each member to EOF is what
        # makes ZipExtFile verify its CRC, so this must be caught here rather
        # than handed to BuildKit as a silently wrong build context.
        _write_zip(self.zip_path, [
            ("service.json", b"{}", _regular()),
            ("big.bin", b"x" * 4096, _regular()),
        ])
        with open(self.zip_path, "rb") as f:
            data = bytearray(f.read())
        data[200] ^= 0xFF  # inside big.bin's stored payload
        with open(self.zip_path, "wb") as f:
            f.write(bytes(data))

        with self.assertRaises(packer.ZipExtractionError) as caught:
            packer.zipfile_ok(zip=self.zip_path)

        message = str(caught.exception)
        self.assertIn("big.bin", message)  # names the member
        self.assertIn(self.zip_path, message)  # and the archive
        self.assertIn("CRC", message)
        self.assertTrue(os.path.exists(self.zip_path))
        self.ok.assert_not_called()

    def test_a_failed_unpack_cleans_the_partial_directory(self):
        _write_zip(self.zip_path, [
            ("service.json", b"{}", _regular()),
            ("../evil", b"pwned\n", _regular()),
        ])

        before = set(os.listdir(self.cache))
        with self.assertRaises(packer.ZipExtractionError):
            packer.zipfile_ok(zip=self.zip_path)

        self.assertEqual(
            set(os.listdir(self.cache)),
            before,
            "the half-written aux directory should be removed",
        )

    def test_a_parent_traversal_member_is_refused(self):
        _write_zip(self.zip_path, [
            ("service.json", b"{}", _regular()),
            ("../evil", b"pwned\n", _regular()),
        ])

        with self.assertRaises(packer.ZipExtractionError) as caught:
            packer.zipfile_ok(zip=self.zip_path)

        self.assertIn("../evil", str(caught.exception))
        self.assertFalse(
            os.path.exists(os.path.join(self.cache, "evil")),
            "zip slip wrote outside the destination",
        )
        # And, again, the input survives so the operator can inspect it.
        self.assertTrue(os.path.exists(self.zip_path))

    def test_a_deep_traversal_member_is_refused(self):
        _write_zip(self.zip_path, [
            ("service.json", b"{}", _regular()),
            ("a/b/../../../../etc/cron.d/x", b"* * * * * root sh\n", _regular()),
        ])

        with self.assertRaises(packer.ZipExtractionError):
            packer.zipfile_ok(zip=self.zip_path)

        self.ok.assert_not_called()

    def test_an_absolute_member_is_refused(self):
        _write_zip(self.zip_path, [
            ("service.json", b"{}", _regular()),
            ("/tmp/nodo-zip-slip-absolute", b"pwned\n", _regular()),
        ])

        with self.assertRaises(packer.ZipExtractionError) as caught:
            packer.zipfile_ok(zip=self.zip_path)

        self.assertIn("absolute", str(caught.exception))
        self.assertFalse(os.path.exists("/tmp/nodo-zip-slip-absolute"))

    def test_a_symlink_member_is_refused(self):
        # nodo's client cannot emit one (`zip -r` without `-y` dereferences, over
        # a tree built by shutil.copytree/copy2, which dereference too), so a
        # symlink means the archive came from somewhere else. extractall would
        # have written the link text into a regular file; we refuse instead.
        _write_zip(self.zip_path, [
            ("service.json", b"{}", _regular()),
            ("passwd", b"/etc/passwd", _symlink()),
        ])

        with self.assertRaises(packer.ZipExtractionError) as caught:
            packer.zipfile_ok(zip=self.zip_path)

        message = str(caught.exception)
        self.assertIn("passwd", message)
        self.assertIn("symbolic link", message)
        self.ok.assert_not_called()

    def test_no_os_system_in_the_unpack_path(self):
        # The point of the change: nothing here shells out. Any reintroduced
        # os.system/unzip would trip this.
        _write_zip(self.zip_path, [("service.json", b"{}", _regular())])

        with mock.patch.object(packer.os, "system") as system:
            packer.zipfile_ok(zip=self.zip_path)

        system.assert_not_called()


if __name__ == "__main__":
    unittest.main()
