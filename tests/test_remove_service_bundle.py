"""`nodo remove` has to free the disk the build took, not just the registry entry.

Removing a service used to clear `REGISTRY/<id>` and `METADATA_REGISTRY/<id>` and
leave the built bundle -- the rootfs image the guest boots, gigabytes for a real
service -- in `CACHE/cloud_hypervisor/<id>/<arch>` for good. The only way to get
that space back was deleting `__cache__` by hand.

Deleting trees in that directory needs a guard, too: `get_id` answers `""` for a
name it cannot resolve, and `""` joined onto a directory is that directory.
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.virtualizers.microvm import build as ch_build
    from src.virtualizers.microvm import paths as microvm_paths
    from src.commands import remove as remove_cmd
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ch_build = None  # type: ignore[assignment]
    microvm_paths = None  # type: ignore[assignment]
    remove_cmd = None  # type: ignore[assignment]

SERVICE_ID = "efe54d0a42af6c989d95ff9bbbadf7e809f80f3c151979ed4a6b19df1412b74d"
OTHER_ID = "fe394b2985f3b1fc24d04be2b92735b1e47fdf68bfd24f9bda9d3d92bfbd7766"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RemoveBuiltServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.bundles = self.cache / microvm_paths.FAMILY_DIR_NAME

        # What a built node looks like: two service bundles, the runtime directories
        # of the VMs that are running, and the debris of a failed launch.
        self.bundle = self._write_bundle(SERVICE_ID, "arm64", size=4096)
        self.other_bundle = self._write_bundle(OTHER_ID, "x86_64", size=512)
        self.runtime = self.bundles / "runtime"
        (self.runtime / SERVICE_ID).mkdir(parents=True)
        (self.runtime / SERVICE_ID / "rootfs.ext4").write_bytes(b"x" * 32)
        self.failures = self.bundles / "failures"
        self.failures.mkdir(parents=True)

        for patcher in (
            patch.object(microvm_paths, "cache_root", return_value=str(self.cache)),
            patch.object(ch_build, "CACHE", str(self.cache)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write_bundle(self, service_id, arch, size):
        bundle_dir = self.bundles / service_id / arch
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "rootfs.ext4").write_bytes(b"0" * size)
        (bundle_dir / "bundle.json").write_text('{"arch": "%s"}' % arch)
        return self.bundles / service_id

    def test_the_bundle_is_deleted_and_its_size_reported(self):
        expected = 4096 + len('{"arch": "arm64"}')

        freed = ch_build.remove_built_service(service_id=SERVICE_ID)

        self.assertEqual(freed, expected)
        self.assertFalse(self.bundle.exists())

    def test_nothing_else_in_the_cache_is_touched(self):
        ch_build.remove_built_service(service_id=SERVICE_ID)

        self.assertTrue(self.other_bundle.is_dir())
        self.assertTrue((self.runtime / SERVICE_ID / "rootfs.ext4").is_file())
        self.assertTrue(self.failures.is_dir())

    def test_a_service_that_was_never_built_frees_nothing(self):
        self.assertEqual(ch_build.remove_built_service(service_id="a" * 64), 0)
        self.assertTrue(self.bundle.is_dir())

    def test_an_empty_id_is_refused_instead_of_deleting_the_cache(self):
        with self.assertRaises(ValueError):
            ch_build.remove_built_service(service_id="")

        self.assertTrue(self.bundle.is_dir())
        self.assertTrue(self.bundles.is_dir())

    def test_the_runtime_and_failure_directories_are_refused_by_name(self):
        for reserved in ("runtime", "failures"):
            with self.subTest(reserved=reserved):
                with self.assertRaises(ValueError):
                    ch_build.remove_built_service(service_id=reserved)
        self.assertTrue(self.runtime.is_dir())
        self.assertTrue(self.failures.is_dir())

    def test_a_path_that_escapes_the_bundle_root_is_refused(self):
        # "/etc" matters: `Path(root) / "/etc"` is `/etc`, not a child of root.
        for escape in ("..", f"../{SERVICE_ID}", f"{SERVICE_ID}/arm64", "/etc"):
            with self.subTest(escape=escape):
                with self.assertRaises(ValueError):
                    ch_build.remove_built_service(service_id=escape)
        self.assertTrue(self.bundle.is_dir())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RemoveCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.registry = root / "__registry__"
        self.metadata = root / "__metadata__"
        for directory in (self.registry, self.metadata):
            directory.mkdir(parents=True)
            (directory / SERVICE_ID).write_bytes(b"service")

        for name, value in (
            ("REGISTRY", str(self.registry)),
            ("METADATA_REGISTRY", str(self.metadata)),
        ):
            patcher = patch.object(remove_cmd, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        patcher = patch.object(remove_cmd.os, "geteuid", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, service, resolved, running=(), freed=0):
        output = io.StringIO()
        with patch.object(remove_cmd, "get_id", return_value=resolved), patch.object(
            remove_cmd, "_running_instances_of", return_value=list(running)
        ), patch(
            "src.virtualizers.interface.remove_built_service", return_value=freed
        ) as remove_bundle, contextlib.redirect_stdout(output):
            remove_cmd.remove(service=service)
        return output.getvalue(), remove_bundle

    def test_removing_a_service_also_removes_its_build(self):
        printed, remove_bundle = self._run(SERVICE_ID, SERVICE_ID, freed=1_500_000_000)

        remove_bundle.assert_called_once_with(service_hash=SERVICE_ID)
        self.assertFalse((self.registry / SERVICE_ID).exists())
        self.assertFalse((self.metadata / SERVICE_ID).exists())
        self.assertIn("1.40 GB", printed)
        self.assertIn("removed from the node", printed)

    def test_a_service_resolved_by_tag_is_removed_by_id(self):
        _printed, remove_bundle = self._run("my-tag", SERVICE_ID, freed=10)

        remove_bundle.assert_called_once_with(service_hash=SERVICE_ID)

    def test_a_service_that_was_never_built_says_so(self):
        printed, _ = self._run(SERVICE_ID, SERVICE_ID, freed=0)

        self.assertIn("No built image was cached", printed)

    def test_an_unknown_name_removes_nothing_at_all(self):
        # `get_id` cannot resolve it, and the registry is not the thing to delete.
        printed, remove_bundle = self._run("not-a-service", "")

        remove_bundle.assert_not_called()
        self.assertTrue((self.registry / SERVICE_ID).is_file())
        self.assertTrue((self.metadata / SERVICE_ID).is_file())
        self.assertTrue(self.registry.is_dir())
        self.assertIn("Nothing was removed", printed)

    def test_running_instances_are_called_out_not_blocked(self):
        printed, remove_bundle = self._run(
            SERVICE_ID, SERVICE_ID, running=["vm-1", "vm-2"], freed=10
        )

        remove_bundle.assert_called_once()
        self.assertIn("2 instance(s)", printed)
        self.assertIn("will rebuild it", printed)

    def test_without_root_nothing_is_removed(self):
        with patch.object(remove_cmd.os, "geteuid", return_value=1000):
            printed, remove_bundle = self._run(SERVICE_ID, SERVICE_ID, freed=10)

        remove_bundle.assert_not_called()
        self.assertTrue((self.registry / SERVICE_ID).is_file())
        self.assertIn("superuser", printed)


if __name__ == "__main__":
    unittest.main()
