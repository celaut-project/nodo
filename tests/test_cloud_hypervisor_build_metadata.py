import os
import stat
import tempfile
import unittest
import importlib
import json
from pathlib import Path
from unittest.mock import patch

from src.utils.filesystem_xattrs import encode_filesystem_metadata_xattrs, FilesystemNodeMetadata

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    ch_build = importlib.import_module("src.virtualizers.ch.build")
    ch_limits = importlib.import_module("src.virtualizers.ch.limits")
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    ch_build = None  # type: ignore[assignment]
    ch_limits = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CloudHypervisorBuildMetadataTests(unittest.TestCase):
    def _security_context(
        self,
        *,
        service_id: str = "svc",
        policy: str = "deny",
        trusted: bool = False,
        require_trusted: bool = True,
        allowlist=(),
        path_confinement: bool = True,
    ):
        return ch_build._BuildSecurityContext(
            service_id=service_id,
            path_confinement=path_confinement,
            device_nodes_policy=policy,
            require_trusted_service_for_devices=require_trusted,
            service_is_trusted_for_devices=trusted,
            device_allowlist=tuple(allowlist),
        )

    def test_validate_branch_name_rejects_dangerous_segments(self):
        with self.assertRaisesRegex(RuntimeError, "empty names"):
            ch_build._validate_branch_name("", "/")
        with self.assertRaisesRegex(RuntimeError, "not allowed"):
            ch_build._validate_branch_name("..", "/")
        with self.assertRaisesRegex(RuntimeError, "'/' is not allowed"):
            ch_build._validate_branch_name("a/b", "/")

    def test_normalize_guest_path_rejects_path_traversal(self):
        with self.assertRaisesRegex(RuntimeError, "path traversal"):
            ch_build._normalize_guest_path("../etc/passwd", "test", allow_root=False)

    def test_safe_rootfs_path_rejects_escape_via_symlink_parent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "rootfs"
            outside = Path(tmpdir) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "dir").mkdir()
            os.symlink(str(outside), root / "dir" / "evil")

            ctx = self._security_context()
            with self.assertRaisesRegex(RuntimeError, "escapes rootfs confinement"):
                ch_build._safe_rootfs_path(root, "/dir/evil/target", ctx)

    def test_decode_branch_metadata_rejects_partial_contract(self):
        branch = celaut.Service.Container.Filesystem.ItemBranch()
        branch.name = "file.txt"
        branch.file = b"hello"
        branch.xattrs["mode"] = str(stat.S_IFREG | 0o644).encode("utf-8")

        with self.assertRaisesRegex(RuntimeError, "Invalid filesystem metadata xattrs"):
            ch_build._decode_branch_metadata(branch, "/file.txt")

    def test_write_item_legacy_file_tracks_fallback_candidates(self):
        branch = celaut.Service.Container.Filesystem.ItemBranch()
        branch.name = "legacy.bin"
        branch.file = b"legacy"

        with tempfile.TemporaryDirectory() as tmpdir:
            symlinks = []
            legacy_regular_files = set()
            with patch.object(ch_build, "copy_block_if_exists", return_value=False):
                ch_build._write_item(
                    branch=branch,
                    root_dir=Path(tmpdir),
                    parent_rel_path="/",
                    symlinks=symlinks,
                    legacy_regular_files=legacy_regular_files,
                    security_context=self._security_context(),
                )

            written = Path(tmpdir) / "legacy.bin"
            self.assertTrue(written.is_file())
            self.assertEqual(symlinks, [])
            self.assertIn(written, legacy_regular_files)

    def test_write_item_rejects_mismatched_link_dst(self):
        branch = celaut.Service.Container.Filesystem.ItemBranch()
        branch.name = "safe-link"
        branch.link.src = "/bin/sh"
        branch.link.dst = "/tmp/other"
        metadata = FilesystemNodeMetadata(
            mode=stat.S_IFLNK | 0o777,
            uid=os.getuid(),
            gid=os.getgid(),
            mtime_ns=1,
            device_major=0,
            device_minor=0,
            device_is_block=False,
        )
        encode_filesystem_metadata_xattrs(branch.xattrs, metadata)

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "Invalid link.dst"):
                ch_build._write_item(
                    branch=branch,
                    root_dir=Path(tmpdir),
                    parent_rel_path="/",
                    symlinks=[],
                    legacy_regular_files=set(),
                    security_context=self._security_context(),
                )

    def test_create_device_node_calls_mknod_with_expected_mode_and_dev(self):
        metadata = FilesystemNodeMetadata(
            mode=stat.S_IFCHR | 0o660,
            uid=0,
            gid=0,
            mtime_ns=0,
            device_major=1,
            device_minor=3,
            device_is_block=False,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            dev_path = Path(tmpdir) / "dev-null"
            with patch.object(ch_build.os, "mknod") as mock_mknod:
                ch_build._create_device_node(
                    dev_path,
                    metadata,
                    "/dev-null",
                    self._security_context(
                        policy="allowlist",
                        trusted=True,
                        require_trusted=True,
                        allowlist=[
                            ch_build._DeviceAllowlistEntry(
                                path="/dev-null",
                                is_block=False,
                                major=1,
                                minor=3,
                                mode=0o660,
                            )
                        ],
                    ),
                )

            expected_dev = os.makedev(1, 3)
            mock_mknod.assert_called_once_with(dev_path, metadata.mode, expected_dev)

    def test_authorize_device_node_denies_by_default_policy(self):
        metadata = FilesystemNodeMetadata(
            mode=stat.S_IFCHR | 0o660,
            uid=0,
            gid=0,
            mtime_ns=0,
            device_major=1,
            device_minor=3,
            device_is_block=False,
        )
        with self.assertRaisesRegex(RuntimeError, "denied by policy"):
            ch_build._authorize_device_node(
                metadata=metadata,
                rel_path="/dev/null",
                security_context=self._security_context(policy="deny"),
            )

    def test_authorize_device_node_requires_trusted_service_when_enabled(self):
        metadata = FilesystemNodeMetadata(
            mode=stat.S_IFCHR | 0o660,
            uid=0,
            gid=0,
            mtime_ns=0,
            device_major=1,
            device_minor=3,
            device_is_block=False,
        )
        allow_entry = ch_build._DeviceAllowlistEntry(
            path="/dev/null",
            is_block=False,
            major=1,
            minor=3,
            mode=0o660,
        )
        with self.assertRaisesRegex(RuntimeError, "untrusted service"):
            ch_build._authorize_device_node(
                metadata=metadata,
                rel_path="/dev/null",
                security_context=self._security_context(
                    policy="allowlist",
                    trusted=False,
                    require_trusted=True,
                    allowlist=[allow_entry],
                ),
            )

    def test_authorize_device_node_blocks_dangerous_mode_bits(self):
        metadata = FilesystemNodeMetadata(
            mode=stat.S_IFCHR | 0o4660,  # setuid
            uid=0,
            gid=0,
            mtime_ns=0,
            device_major=1,
            device_minor=3,
            device_is_block=False,
        )
        allow_entry = ch_build._DeviceAllowlistEntry(
            path="/dev/null",
            is_block=False,
            major=1,
            minor=3,
            mode=0o660,
        )
        with self.assertRaisesRegex(RuntimeError, "forbidden mode bits"):
            ch_build._authorize_device_node(
                metadata=metadata,
                rel_path="/dev/null",
                security_context=self._security_context(
                    policy="allowlist",
                    trusted=True,
                    require_trusted=True,
                    allowlist=[allow_entry],
                ),
            )

    def test_apply_regular_metadata_fails_strictly_on_chown_error(self):
        with tempfile.NamedTemporaryFile() as tmp:
            path = Path(tmp.name)
            metadata = FilesystemNodeMetadata(
                mode=stat.S_IFREG | 0o644,
                uid=os.getuid() + 1,
                gid=os.getgid(),
                mtime_ns=int(path.stat().st_mtime_ns),
                device_major=0,
                device_minor=0,
                device_is_block=False,
            )

            with patch.object(ch_build.os, "lchown", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(RuntimeError, "Failed to apply ownership"):
                    ch_build._apply_regular_metadata(path, metadata, "/tmp-file")

    def test_apply_chmod_fails_when_secure_nofollow_not_supported(self):
        with tempfile.NamedTemporaryFile() as tmp:
            path = Path(tmp.name)
            with patch.object(ch_build.os, "chmod", side_effect=TypeError("no-follow unsupported")):
                with self.assertRaisesRegex(RuntimeError, "secure no-follow chmod is not supported"):
                    ch_build._apply_chmod(path, stat.S_IFREG | 0o644, "/tmp-file")

    def test_apply_symlink_preserves_metadata_when_available(self):
        branch = celaut.Service.Container.Filesystem.ItemBranch()
        branch.name = "app-link"
        branch.link.src = "/bin/app"
        branch.link.dst = "/app-link"
        metadata = FilesystemNodeMetadata(
            mode=stat.S_IFLNK | 0o777,
            uid=os.getuid(),
            gid=os.getgid(),
            mtime_ns=1712000000000000000,
            device_major=0,
            device_minor=0,
            device_is_block=False,
        )
        encode_filesystem_metadata_xattrs(branch.xattrs, metadata)

        with tempfile.TemporaryDirectory() as tmpdir:
            root_dir = Path(tmpdir)
            symlinks = []
            legacy_regular_files = set()
            ch_build._write_item(
                branch=branch,
                root_dir=root_dir,
                parent_rel_path="/",
                symlinks=symlinks,
                legacy_regular_files=legacy_regular_files,
                security_context=self._security_context(),
            )

            with patch.object(ch_build.os, "chown"), patch.object(ch_build.os, "utime"):
                ch_build._apply_symlinks(
                    symlinks=symlinks,
                    root_dir=root_dir,
                    security_context=self._security_context(),
                )

            self.assertTrue((root_dir / "app-link").is_symlink())

    def test_resolve_initial_rootfs_size_bytes_respects_requested_disk_space(self):
        service = celaut.Service(
            container=celaut.Service.Container(
                resources=celaut.Service.Container.Resources(
                    at_init=celaut.Sysresources(disk_space=256),
                    at_most=celaut.Sysresources(disk_space=4096),
                )
            )
        )

        size_bytes = ch_limits.initial_rootfs_size_bytes(
            service=service,
            total_bytes=1024,
        )

        self.assertEqual(size_bytes, 4096)

    def test_resolve_initial_rootfs_size_bytes_keeps_filesystem_overhead_floor(self):
        service = celaut.Service(
            container=celaut.Service.Container(
                resources=celaut.Service.Container.Resources(
                    at_most=celaut.Sysresources(disk_space=1024),
                )
            )
        )

        total_bytes = 10 * 1024 * 1024
        size_bytes = ch_limits.initial_rootfs_size_bytes(
            service=service,
            total_bytes=total_bytes,
        )

        self.assertEqual(size_bytes, total_bytes + ch_limits.OVERHEAD_BYTES)

    def test_is_service_built_for_arch_rejects_bundle_smaller_than_requested_disk(self):
        service = celaut.Service(
            container=celaut.Service.Container(
                resources=celaut.Service.Container.Resources(
                    at_most=celaut.Sysresources(disk_space=4096),
                )
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_dir = Path(tmpdir) / "cloud_hypervisor" / "svc" / "x86_64"
            bundle_dir.mkdir(parents=True)
            (bundle_dir / "rootfs.ext4").write_bytes(b"x" * 2048)
            with open(bundle_dir / "bundle.json", "w", encoding="utf-8") as f:
                json.dump({"rootfs_size_bytes": 2048}, f)

            with patch.object(ch_build, "CACHE", tmpdir):
                self.assertFalse(
                    ch_build._is_service_built_for_arch("svc", "x86_64", service=service)
                )

    def test_is_service_built_for_arch_accepts_bundle_large_enough_for_requested_disk(self):
        service = celaut.Service(
            container=celaut.Service.Container(
                resources=celaut.Service.Container.Resources(
                    at_most=celaut.Sysresources(disk_space=4096),
                )
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_dir = Path(tmpdir) / "cloud_hypervisor" / "svc" / "x86_64"
            bundle_dir.mkdir(parents=True)
            (bundle_dir / "rootfs.ext4").write_bytes(b"x" * 8192)
            with open(bundle_dir / "bundle.json", "w", encoding="utf-8") as f:
                json.dump({"rootfs_size_bytes": 8192}, f)

            with patch.object(ch_build, "CACHE", tmpdir):
                self.assertTrue(
                    ch_build._is_service_built_for_arch("svc", "x86_64", service=service)
                )


if __name__ == "__main__":
    unittest.main()
