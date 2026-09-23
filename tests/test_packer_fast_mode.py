"""`nodo pack --fast` / `--optimize` / `packer.fast` (single-block packing).

The local packer (`packer.local: true`) normally splits every file at or over
`packer.MIN_BUFFER_BLOCK_SIZE` into its own content-addressed block under
`main.BLOCKDIR`, inlining everything smaller into the service's filesystem
message. `--fast` skips that entirely -- every file inlines, so the filesystem
ends up as a single block with nothing else in `BLOCKDIR`.

These tests pin the plumbing that carries the `fast` flag from `nodo pack`
down to the actual per-file decision, without running BuildKit:

* `_is_inline` -- the threshold check itself, unit-tested directly;
* `pack_zip` -- forwards `fast` to the worker subprocess as a trailing
  `--fast` argv entry (the worker is a separate process, so it can't be
  passed as a Python kwarg);
* `_worker_main` -- parses that trailing `--fast` back out;
* `pack()` (`pack.py`) -- forwards `fast` to the local packer, and warns +
  ignores it on the (default) remote packer-service backend, which this repo
  does not control the internals of.

`ZipContainerPacker.__init__` runs BuildKit, so `parseFilesys`'s actual block
creation is exercised only through `_is_inline` here -- the same test shape
`test_packer_read_only_filesystem.py` uses for the other init-only-tested
methods.
"""
import os
import tempfile
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    # Before anything that builds a ConfigManager at import: the shipped example
    # points STORAGE at /nodo, which only exists on an installed node.
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.packers import zip_with_dockerfile as packer
    from src.commands.packer.zip_with_dockerfile import pack as pack_mod
    from src.commands.packer.zip_with_dockerfile import local_pack as local_pack_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    packer = None  # type: ignore[assignment]
    pack_mod = None  # type: ignore[assignment]
    local_pack_mod = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class IsInlineTests(unittest.TestCase):
    def test_fast_inlines_regardless_of_size(self):
        huge = packer.MIN_BUFFER_BLOCK_SIZE * 100
        self.assertTrue(packer._is_inline(huge, fast=True))

    def test_normal_mode_follows_the_threshold(self):
        small = packer.MIN_BUFFER_BLOCK_SIZE - 1
        big = packer.MIN_BUFFER_BLOCK_SIZE
        self.assertTrue(packer._is_inline(small, fast=False))
        self.assertFalse(packer._is_inline(big, fast=False))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ZipfileOkForwardsFastTests(unittest.TestCase):
    """zipfile_ok() -> ok(): fast must reach ZipContainerPacker unchanged."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo packer fast test-")
        self.addCleanup(self._tmp.cleanup)
        self.cache = os.path.join(self._tmp.name, "cache") + os.sep
        os.makedirs(self.cache, exist_ok=True)

        cache_patch = mock.patch.object(packer, "CACHE", self.cache)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

        self.ok = mock.Mock(return_value=("serviceid", None, "/dev/null"))
        ok_patch = mock.patch.object(packer, "ok", self.ok)
        ok_patch.start()
        self.addCleanup(ok_patch.stop)

        self.zip_path = os.path.join(self._tmp.name, "service.zip")
        import stat
        import zipfile
        with zipfile.ZipFile(self.zip_path, "w") as z:
            info = zipfile.ZipInfo("service.json")
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(info, "{}")

    def test_fast_true_reaches_ok(self):
        packer.zipfile_ok(zip=self.zip_path, fast=True)
        self.assertTrue(self.ok.call_args.kwargs["fast"])

    def test_fast_defaults_to_false(self):
        packer.zipfile_ok(zip=self.zip_path)
        self.assertFalse(self.ok.call_args.kwargs["fast"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PackZipWorkerCommandTests(unittest.TestCase):
    """pack_zip() spawns the worker subprocess -- fast rides along as argv."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo packer fast test-")
        self.addCleanup(self._tmp.cleanup)
        self.cache = os.path.join(self._tmp.name, "cache") + os.sep
        os.makedirs(self.cache, exist_ok=True)
        cache_patch = mock.patch.object(packer, "CACHE", self.cache)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def _worker_cmd(self, fast):
        # A non-zero returncode short-circuits pack_zip into the "subprocess
        # failed" branch before it ever touches a result file, so no worker
        # process or result JSON needs to exist for this test.
        run_mock = mock.Mock(return_value=mock.Mock(returncode=1))
        with mock.patch("subprocess.run", run_mock):
            list(packer.pack_zip(zip=os.path.join(self._tmp.name, "s.zip"), fast=fast))
        run_mock.assert_called_once()
        return run_mock.call_args.args[0]

    def test_fast_true_appends_the_flag(self):
        cmd = self._worker_cmd(fast=True)
        self.assertIn("--fast", cmd)

    def test_fast_false_omits_the_flag(self):
        cmd = self._worker_cmd(fast=False)
        self.assertNotIn("--fast", cmd)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WorkerMainParsesFastTests(unittest.TestCase):
    """_worker_main() parses the trailing --fast back out of argv."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo packer fast test-")
        self.addCleanup(self._tmp.cleanup)
        self.result_path = os.path.join(self._tmp.name, "result.json")

        self.zipfile_ok = mock.Mock(return_value=("serviceid", None, "/dev/null"))
        patch_ = mock.patch.object(packer, "zipfile_ok", self.zipfile_ok)
        patch_.start()
        self.addCleanup(patch_.stop)

    def _run(self, extra_argv):
        argv = ["nodo-worker", "--worker", "z.zip", self.result_path] + extra_argv
        with mock.patch.object(packer.sys, "argv", argv):
            packer._worker_main()
        return self.zipfile_ok.call_args.kwargs["fast"]

    def test_trailing_fast_flag_is_parsed(self):
        self.assertTrue(self._run(["--fast"]))

    def test_no_flag_defaults_to_false(self):
        self.assertFalse(self._run([]))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PackDispatchTests(unittest.TestCase):
    """pack() (pack.py): local backend forwards fast; remote backend warns."""

    def test_local_backend_forwards_fast_to_pack_local(self):
        pack_local_mock = mock.Mock(return_value="serviceid")
        with mock.patch.object(pack_mod, "_local_packer_enabled", return_value=True), \
             mock.patch.object(local_pack_mod, "pack_local", pack_local_mock):
            result = pack_mod.pack("some/dir", fast=True)

        self.assertEqual(result, "serviceid")
        pack_local_mock.assert_called_once_with("some/dir", fast=True)

    def test_remote_backend_warns_and_ignores_fast(self):
        via_service = mock.Mock(return_value="serviceid")
        with mock.patch.object(pack_mod, "_local_packer_enabled", return_value=False), \
             mock.patch.object(pack_mod, "_pack_via_service", via_service), \
             mock.patch("builtins.print") as mock_print:
            result = pack_mod.pack("some/dir", fast=True)

        self.assertEqual(result, "serviceid")
        via_service.assert_called_once_with("some/dir")
        self.assertTrue(
            any("--fast" in str(call) for call in mock_print.call_args_list),
            f"expected a --fast warning, got: {mock_print.call_args_list}",
        )

    def test_remote_backend_without_fast_is_silent(self):
        via_service = mock.Mock(return_value="serviceid")
        with mock.patch.object(pack_mod, "_local_packer_enabled", return_value=False), \
             mock.patch.object(pack_mod, "_pack_via_service", via_service), \
             mock.patch("builtins.print") as mock_print:
            pack_mod.pack("some/dir", fast=False)

        mock_print.assert_not_called()


if __name__ == "__main__":
    unittest.main()
