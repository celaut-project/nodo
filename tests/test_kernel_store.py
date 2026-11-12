"""The guest kernel store indexed by hash (#536, phase P2).

A guest-kernel update must never change the kernel of a built service: the bundle
names its kernel by hash, and the node never boots it with another kernel.
"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.virtualizers.microvm import bundle as microvm_bundle
from src.virtualizers.microvm import kernel_store
from src.virtualizers.microvm import paths


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Node:
    """A temporary MAIN_DIR + CACHE with the config keys that the store reads."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.main = self.root / "nodo"
        self.cache = self.root / "cache"
        self.legacy = self.main / "cloud_hypervisor" / "kernels" / "linux" / "amd64" / "vmlinuz"
        self.legacy.parent.mkdir(parents=True)
        self.cache.mkdir()
        self.config = {
            "main.MAIN_DIR": str(self.main),
            "CACHE": str(self.cache),
            "virtualizers.ch.KERNEL_PATHS": {"linux/amd64": str(self.legacy)},
            "virtualizers.ch.KERNEL_ABI_ALIASES": {},
        }
        self.patches = [
            patch.object(kernel_store.env_manager, "get", side_effect=self.get),
            patch.object(paths.env_manager, "get", side_effect=self.get),
        ]

    def get(self, key, default=None):
        return self.config.get(key, default)

    def __enter__(self):
        kernel_store._HASH_CACHE.clear()
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write_kernel(self, path: Path, data: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            os.chmod(path, 0o644)
        path.write_bytes(data)

    def old_bundle(self, service_id="s" * 64, arch="linux/amd64"):
        """A bundle.json as builds wrote it before the store: a path, no hash."""
        bdir = self.cache / "microvm" / service_id / arch
        bdir.mkdir(parents=True)
        for name in ("rootfs.ext4", "initramfs"):
            (bdir / name).write_bytes(b"x")
        manifest = {
            "service_id": service_id,
            "arch": arch,
            "rootfs_path": str(bdir / "rootfs.ext4"),
            "rootfs_format": "ext4",
            "kernel_path": str(self.legacy),
            "initramfs_path": str(bdir / "initramfs"),
        }
        (bdir / "bundle.json").write_text(json.dumps(manifest))
        return bdir / "bundle.json"


class KernelStoreTests(unittest.TestCase):
    def test_ingest_is_by_hash_and_read_only(self):
        with _Node() as node:
            node.write_kernel(node.legacy, b"kernel-A")
            h = kernel_store.ingest(node.legacy)
            self.assertEqual(h, _sha(b"kernel-A"))
            stored = kernel_store.store_path(h)
            self.assertTrue(stored.is_file())
            self.assertEqual(stored.read_bytes(), b"kernel-A")
            self.assertEqual(stored.stat().st_mode & 0o777, 0o444)
            # Twice is a no-op.
            self.assertEqual(kernel_store.ingest(node.legacy), h)

    def test_build_records_the_hash_and_boots_the_store_copy(self):
        with _Node() as node:
            node.write_kernel(node.legacy, b"kernel-A")
            h, path = kernel_store.kernel_for_build(str(node.legacy))
            self.assertEqual(h, _sha(b"kernel-A"))
            self.assertEqual(path, str(kernel_store.store_path(h)))

    def test_store_not_writable_records_the_hash_and_resolves_by_content(self):
        with _Node() as node:
            node.write_kernel(node.legacy, b"kernel-A")
            with patch.object(kernel_store, "ingest", side_effect=PermissionError("ro")):
                h, path = kernel_store.kernel_for_build(str(node.legacy))
            self.assertEqual(path, str(node.legacy))
            self.assertEqual(kernel_store.resolve(h), node.legacy)

    def test_an_update_of_the_legacy_file_is_never_booted_for_an_old_hash(self):
        with _Node() as node:
            node.write_kernel(node.legacy, b"kernel-A")
            with patch.object(kernel_store, "ingest", side_effect=PermissionError("ro")):
                h, _ = kernel_store.kernel_for_build(str(node.legacy))
            node.write_kernel(node.legacy, b"kernel-B, a newer and longer one")
            with self.assertRaises(kernel_store.KernelNotFound) as ctx:
                kernel_store.resolve(h)
            self.assertIn(h, str(ctx.exception))

    def test_abi_alias_resolves_an_old_hash_to_the_claiming_kernel(self):
        with _Node() as node:
            node.write_kernel(node.legacy, b"kernel-B")
            new = kernel_store.ingest(node.legacy)
            old = _sha(b"kernel-A")
            node.config["virtualizers.ch.KERNEL_ABI_ALIASES"] = {new: [old, "junk"]}
            self.assertEqual(kernel_store.resolve(old), kernel_store.store_path(new))
            self.assertEqual(kernel_store.abi_aliases(), {new: [old]})

    def test_invalid_hash_is_refused(self):
        with _Node():
            with self.assertRaises(kernel_store.KernelNotFound):
                kernel_store.resolve("../../etc/passwd")


class BundleMigrationTests(unittest.TestCase):
    def test_old_bundle_is_pinned_at_load_and_survives_a_kernel_update(self):
        with _Node() as node:
            node.write_kernel(node.legacy, b"kernel-A")
            bundle_path = node.old_bundle()
            loaded = microvm_bundle.load_bundle("s" * 64, "linux/amd64")
            h = _sha(b"kernel-A")
            self.assertEqual(loaded["kernel_hash"], h)
            self.assertEqual(loaded["kernel_path"], str(kernel_store.store_path(h)))
            on_disk = json.loads(bundle_path.read_text())
            self.assertEqual(on_disk["kernel_hash"], h)
            self.assertEqual(on_disk["kernel_hash_algorithm"], kernel_store.STORE_ALGORITHM_HEX)
            # An update (the old installer's fixed path rewritten) does not move it.
            node.write_kernel(node.legacy, b"kernel-B")
            again = microvm_bundle.load_bundle("s" * 64, "linux/amd64")
            self.assertEqual(again["kernel_path"], str(kernel_store.store_path(h)))
            self.assertEqual(Path(again["kernel_path"]).read_bytes(), b"kernel-A")

    def test_bundle_with_a_missing_kernel_hash_fails_with_the_hash(self):
        with _Node() as node:
            node.write_kernel(node.legacy, b"kernel-A")
            bundle_path = node.old_bundle()
            manifest = json.loads(bundle_path.read_text())
            manifest["kernel_hash"] = _sha(b"gone")
            bundle_path.write_text(json.dumps(manifest))
            with self.assertRaises(kernel_store.KernelNotFound) as ctx:
                microvm_bundle.load_bundle("s" * 64, "linux/amd64")
            self.assertIn(_sha(b"gone"), str(ctx.exception))

    def test_migrate_all_bundles_is_idempotent(self):
        with _Node() as node:
            node.write_kernel(node.legacy, b"kernel-A")
            node.old_bundle("a" * 64)
            node.old_bundle("b" * 64)
            first = kernel_store.migrate_all_bundles()
            self.assertEqual(first, {"bundles": 2, "migrated": 2, "kernels_ingested": 1})
            second = kernel_store.migrate_all_bundles()
            self.assertEqual(second, {"bundles": 2, "migrated": 0, "kernels_ingested": 0})
            report = kernel_store.bundle_kernel_report()
            self.assertEqual(report["by_hash"], {_sha(b"kernel-A"): 2})
            self.assertEqual(report["unstamped"], [])
            self.assertEqual(report["unresolved"], {})
            listing = kernel_store.store_listing()
            self.assertEqual([k["hash"] for k in listing], [_sha(b"kernel-A")])

    def test_report_names_bundles_whose_kernel_is_gone(self):
        with _Node() as node:
            node.write_kernel(node.legacy, b"kernel-A")
            bundle_path = node.old_bundle()
            manifest = json.loads(bundle_path.read_text())
            manifest["kernel_hash"] = _sha(b"gone")
            bundle_path.write_text(json.dumps(manifest))
            report = kernel_store.bundle_kernel_report()
            self.assertEqual(report["unresolved"], {_sha(b"gone"): ["s" * 16 + "/linux/amd64"]})


class InstallerTests(unittest.TestCase):
    def test_setup_scripts_install_into_the_store_and_never_the_fixed_path(self):
        for script in ("bash/setup_linux_x86.sh", "bash/setup_linux_arm.sh"):
            text = Path(script).read_text(encoding="utf-8")
            with self.subTest(script=script):
                self.assertIn('kernel_target="${kernel_store}/${kernel_digest}/vmlinuz"', text)
                self.assertNotIn("kernels/${arch_tag}/vmlinuz", text)


if __name__ == "__main__":
    unittest.main()
