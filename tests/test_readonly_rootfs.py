"""A `read_mode=ro` service is built, priced and booted as an immutable image.

Issue #369: a packed service could only ever be a writable, pre-sized ext4 image,
which carried a floor of `max(128 MiB, tree + 64 MiB)`. That floor exists to leave
room for writes, and a content-addressed service has none to leave room for -- so
it was 64 MiB of flat tax on every capsule, 44% of a 145 MiB one and 10x on a
12 MB one.

The contract is `Filesystem.xattrs["read_mode"]`, and these are the four places it
has to be honoured consistently or a service is built one way and booted another:

* the helper that reads it (absent is `rw`, garbage is an error, never a default),
* the completeness gate that makes `ro` safe (no legacy exec-sniffing fallback),
* the builder (which mkfs runs, what bundle.json records, what disk_space means),
* the boot path (cmdline `ro`/`rootfstype=`, and /init's mount).
"""
import importlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils.filesystem_xattrs import (
    FILESYSTEM_METADATA_KEYS,
    FilesystemNodeMetadata,
    READ_MODE_RO,
    READ_MODE_RW,
    assert_complete_filesystem_metadata,
    encode_filesystem_metadata_xattrs,
    read_mode,
)
from src.virtualizers.microvm import bundle_formats

# Two guards, not one. `limits` is dependency-light on purpose (protos + config,
# no block registry, no hypervisor) precisely so pricing stays testable on a bare
# checkout, and `build` is not -- it pulls in bee_rpc. Collapsing them into one
# skip would take the sizing and gating tests down with the builder on any host
# missing a packer dependency, which is most of them.
LIMITS_IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    ch_limits = importlib.import_module("src.virtualizers.microvm.limits")
except Exception as import_exc:  # pragma: no cover - environment-dependent
    LIMITS_IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    ch_limits = None  # type: ignore[assignment]

BUILD_IMPORT_ERROR = None
try:
    ch_build = importlib.import_module("src.virtualizers.microvm.build")
    microvm_paths = importlib.import_module("src.virtualizers.microvm.paths")
except Exception as import_exc:  # pragma: no cover - environment-dependent
    BUILD_IMPORT_ERROR = import_exc
    ch_build = None  # type: ignore[assignment]
    microvm_paths = None  # type: ignore[assignment]

# A third: the two launch paths need grpc and a writable storage directory, which
# is what the existing execute-helper tests guard on too.
EXECUTE_IMPORT_ERROR = None
try:
    from src.virtualizers.ch import execute as ch_execute
    from src.virtualizers.qemu import execute as qemu_execute
except Exception as import_exc:  # pragma: no cover - environment-dependent
    EXECUTE_IMPORT_ERROR = import_exc
    ch_execute = None  # type: ignore[assignment]
    qemu_execute = None  # type: ignore[assignment]

MIB = 1024 * 1024


def _metadata(mode=stat.S_IFREG | 0o644, uid=0, gid=0):
    return FilesystemNodeMetadata(
        mode=mode,
        uid=uid,
        gid=gid,
        mtime_ns=0,
        device_major=0,
        device_minor=0,
        device_is_block=False,
    )


def _complete_branch(name="file.txt", content=b"hello", mode=stat.S_IFREG | 0o644):
    branch = celaut.Service.Container.Filesystem.ItemBranch()
    branch.name = name
    branch.file = content
    encode_filesystem_metadata_xattrs(branch.xattrs, _metadata(mode=mode))
    return branch


def _ro_filesystem(*branches):
    fs = celaut.Service.Container.Filesystem()
    fs.xattrs["read_mode"] = b"ro"
    for branch in branches:
        fs.branch.append(branch)
    return fs


def _service(filesystem=None, disk_space=None, scope="at_init"):
    container = celaut.Service.Container()
    if filesystem is not None:
        container.filesystem = filesystem.SerializeToString()
    if disk_space is not None:
        resources = celaut.Service.Container.Resources()
        getattr(resources, scope).disk_space = disk_space
        container.resources.CopyFrom(resources)
    return celaut.Service(container=container)


# ---------------------------------------------------------------- point 1 + 3


@unittest.skipIf(LIMITS_IMPORT_ERROR is not None, f"Missing runtime dependencies: {LIMITS_IMPORT_ERROR}")
class ReadModeHelperTests(unittest.TestCase):
    """`read_mode` answers the only three things it may: rw, ro, or refusal."""

    def test_a_filesystem_that_says_nothing_is_writable(self):
        # Every service packed before this key existed says nothing, so this is
        # what keeps the change from touching any of them.
        self.assertEqual(read_mode(celaut.Service.Container.Filesystem()), READ_MODE_RW)

    def test_an_explicit_rw_is_writable(self):
        fs = celaut.Service.Container.Filesystem()
        fs.xattrs["read_mode"] = b"rw"
        self.assertEqual(read_mode(fs), READ_MODE_RW)

    def test_ro_is_read_only(self):
        self.assertEqual(read_mode(_ro_filesystem()), READ_MODE_RO)

    def test_an_unrecognised_value_is_refused_rather_than_defaulted(self):
        # The direction that matters: a typo must not resolve to "rw" and build a
        # service the other way round from the one its author declared.
        fs = celaut.Service.Container.Filesystem()
        fs.xattrs["read_mode"] = b"readonly"
        with self.assertRaisesRegex(ValueError, "unsupported read_mode"):
            read_mode(fs)

    def test_a_non_utf8_value_is_refused(self):
        fs = celaut.Service.Container.Filesystem()
        fs.xattrs["read_mode"] = b"\xff\xfe"
        with self.assertRaisesRegex(ValueError, "not valid UTF-8"):
            read_mode(fs)

    def test_only_the_root_filesystems_read_mode_is_read(self):
        # A nested Filesystem is a subdirectory. It is not separately mounted, so
        # a read_mode on one describes nothing -- and must not be able to turn a
        # writable service read-only from inside its own tree.
        nested = _ro_filesystem()
        branch = celaut.Service.Container.Filesystem.ItemBranch()
        branch.name = "subdir"
        branch.filesystem = nested.SerializeToString()

        root = celaut.Service.Container.Filesystem()
        root.branch.append(branch)

        self.assertEqual(read_mode(root), READ_MODE_RW)


# -------------------------------------------------------------------- point 2


@unittest.skipIf(LIMITS_IMPORT_ERROR is not None, f"Missing runtime dependencies: {LIMITS_IMPORT_ERROR}")
class MetadataCompletenessGateTests(unittest.TestCase):
    """`ro` makes the metadata keys mandatory; `rw` keeps the legacy fallback."""

    def test_a_fully_declared_tree_passes(self):
        assert_complete_filesystem_metadata(_ro_filesystem(_complete_branch()))

    def test_an_entry_with_no_metadata_at_all_is_refused(self):
        # This is precisely the legacy shape: no xattrs, an exec bit guessed from
        # a shebang or ELF magic. Fine for an image the guest can repair, not for
        # one it cannot.
        bare = celaut.Service.Container.Filesystem.ItemBranch()
        bare.name = "legacy.bin"
        bare.file = b"#!/bin/sh\n"

        with self.assertRaisesRegex(ValueError, "incomplete filesystem metadata"):
            assert_complete_filesystem_metadata(_ro_filesystem(bare))

    def test_the_refusal_names_the_path_and_the_missing_keys(self):
        partial = celaut.Service.Container.Filesystem.ItemBranch()
        partial.name = "half.txt"
        partial.file = b"x"
        partial.xattrs["mode"] = str(stat.S_IFREG | 0o644).encode("utf-8")

        with self.assertRaises(ValueError) as ctx:
            assert_complete_filesystem_metadata(_ro_filesystem(partial))

        message = str(ctx.exception)
        self.assertIn("/half.txt", message)
        for key in ("uid", "gid", "mtime_ns"):
            self.assertIn(key, message)

    def test_the_gate_reaches_into_subdirectories(self):
        # Everything in a subdirectory ends up in the image just as much as the
        # root's entries do, so checking only the top level would let exactly the
        # thing this refuses through.
        bare = celaut.Service.Container.Filesystem.ItemBranch()
        bare.name = "deep.bin"
        bare.file = b"x"

        nested = celaut.Service.Container.Filesystem()
        nested.branch.append(bare)

        subdir = celaut.Service.Container.Filesystem.ItemBranch()
        subdir.name = "usr"
        subdir.filesystem = nested.SerializeToString()
        encode_filesystem_metadata_xattrs(
            subdir.xattrs, _metadata(mode=stat.S_IFDIR | 0o755)
        )

        with self.assertRaisesRegex(ValueError, "/usr/deep.bin"):
            assert_complete_filesystem_metadata(_ro_filesystem(subdir))

    def test_every_contract_key_is_required(self):
        for missing_key in FILESYSTEM_METADATA_KEYS:
            with self.subTest(missing=missing_key):
                branch = _complete_branch()
                del branch.xattrs[missing_key]
                with self.assertRaises(ValueError):
                    assert_complete_filesystem_metadata(_ro_filesystem(branch))

    def test_a_writable_service_still_takes_the_legacy_fallback(self):
        # The gate is not applied on the rw path at all: build() only calls it for
        # read_mode=ro. Asserting it here as the contract, since a tree with no
        # metadata is what every pre-contract service looks like.
        bare = celaut.Service.Container.Filesystem.ItemBranch()
        bare.name = "legacy.bin"
        bare.file = b"#!/bin/sh\n"

        fs = celaut.Service.Container.Filesystem()
        fs.branch.append(bare)

        self.assertEqual(read_mode(fs), READ_MODE_RW)


# -------------------------------------------------------------------- point 3


@unittest.skipIf(BUILD_IMPORT_ERROR is not None, f"Missing runtime dependencies: {BUILD_IMPORT_ERROR}")
class ReadOnlyFormatSelectionTests(unittest.TestCase):
    """Which mkfs runs is the node's choice, and a missing tool says so."""

    def test_erofs_is_preferred_when_both_are_available(self):
        with patch.object(ch_build, "ROOTFS_READ_ONLY_FORMAT", "auto"), \
             patch.object(ch_build.shutil, "which", side_effect=lambda b: f"/usr/bin/{b}"):
            self.assertEqual(
                ch_build._select_read_only_format(), bundle_formats.ROOTFS_FORMAT_EROFS
            )

    def test_squashfs_is_used_when_erofs_utils_is_absent(self):
        def which(binary):
            return None if binary == "mkfs.erofs" else f"/usr/bin/{binary}"

        with patch.object(ch_build, "ROOTFS_READ_ONLY_FORMAT", "auto"), \
             patch.object(ch_build.shutil, "which", side_effect=which):
            self.assertEqual(
                ch_build._select_read_only_format(),
                bundle_formats.ROOTFS_FORMAT_SQUASHFS,
            )

    def test_neither_tool_names_both_in_the_error(self):
        # A node that cannot build the image has to say what to install, not just
        # that something failed.
        with patch.object(ch_build, "ROOTFS_READ_ONLY_FORMAT", "auto"), \
             patch.object(ch_build.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                ch_build._select_read_only_format()

        message = str(ctx.exception)
        self.assertIn("mkfs.erofs", message)
        self.assertIn("mksquashfs", message)

    def test_an_operator_can_pin_a_format(self):
        with patch.object(ch_build, "ROOTFS_READ_ONLY_FORMAT", "squashfs"), \
             patch.object(ch_build.shutil, "which", side_effect=lambda b: f"/usr/bin/{b}"):
            self.assertEqual(
                ch_build._select_read_only_format(),
                bundle_formats.ROOTFS_FORMAT_SQUASHFS,
            )

    def test_an_unknown_pinned_format_is_refused(self):
        with patch.object(ch_build, "ROOTFS_READ_ONLY_FORMAT", "btrfs"):
            with self.assertRaisesRegex(RuntimeError, "ROOTFS_READ_ONLY_FORMAT"):
                ch_build._select_read_only_format()


@unittest.skipIf(BUILD_IMPORT_ERROR is not None, f"Missing runtime dependencies: {BUILD_IMPORT_ERROR}")
class ReadOnlyMkfsInvocationTests(unittest.TestCase):
    """The two new mkfs wrappers, in the style of `_mkfs_ext4`.

    mksquashfs/mkfs.erofs are not installed on every dev machine (nor on macOS at
    all), so the subprocess is mocked exactly the way the ext4 tests mock theirs.
    What is asserted is the argv -- which is the part that has to be right, since
    a wrong flag here is an image with the wrong ownership inside it.
    """

    def test_mksquashfs_passes_no_size_and_never_flattens_ownership(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "rootfs.squashfs"

            def fake_run(cmd, **kwargs):
                image.write_bytes(b"x" * 4096)
                return unittest.mock.Mock(returncode=0, stdout="", stderr="")

            with patch.object(ch_build.subprocess, "run", side_effect=fake_run) as run:
                size = ch_build._mksquashfs(Path(tmpdir), image)

            argv = run.call_args[0][0]
            self.assertEqual(argv[0], "mksquashfs")
            self.assertIn("-noappend", argv)
            # -all-root would rewrite every uid/gid to 0, discarding exactly what
            # the metadata gate above exists to guarantee.
            self.assertNotIn("-all-root", argv)
            # No size argument anywhere: the image is its contents, which is the
            # whole point of the read-only path.
            self.assertNotIn("-b", argv)
            self.assertEqual(size, 4096)

    def test_mkfs_erofs_takes_image_then_directory_and_compresses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "rootfs.erofs"

            def fake_run(cmd, **kwargs):
                image.write_bytes(b"x" * 2048)
                return unittest.mock.Mock(returncode=0, stdout="", stderr="")

            with patch.object(ch_build.subprocess, "run", side_effect=fake_run) as run:
                size = ch_build._mkfs_erofs(Path(tmpdir), image)

            argv = run.call_args[0][0]
            self.assertEqual(argv[0], "mkfs.erofs")
            # mkfs.erofs takes <image> <source-dir>, in that order.
            self.assertEqual(argv[-2:], [str(image), str(tmpdir)])
            self.assertTrue(any(a.startswith("-z") for a in argv))
            self.assertEqual(size, 2048)

    def test_a_missing_mksquashfs_names_the_package_to_install(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ch_build.subprocess, "run", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(RuntimeError, "mksquashfs not found in PATH"):
                    ch_build._mksquashfs(Path(tmpdir), Path(tmpdir) / "img")

    def test_a_missing_mkfs_erofs_names_the_package_to_install(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ch_build.subprocess, "run", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(RuntimeError, "mkfs.erofs not found in PATH"):
                    ch_build._mkfs_erofs(Path(tmpdir), Path(tmpdir) / "img")

    def test_a_failing_tool_surfaces_its_own_stderr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            error = ch_build.subprocess.CalledProcessError(
                1, ["mkfs.erofs"], output="", stderr="cannot read source"
            )
            with patch.object(ch_build.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, "cannot read source"):
                    ch_build._mkfs_erofs(Path(tmpdir), Path(tmpdir) / "img")


# ------------------------------------------------------------------- point 4


@unittest.skipIf(LIMITS_IMPORT_ERROR is not None, f"Missing runtime dependencies: {LIMITS_IMPORT_ERROR}")
class BundleFormatTests(unittest.TestCase):
    """bundle.json records what was built; an old bundle still reads as ext4."""

    def test_a_bundle_without_the_key_is_ext4(self):
        # Every bundle written before this change. Defaulting any other way stops
        # an upgraded node booting everything it had already built.
        self.assertEqual(
            bundle_formats.rootfs_format_of({"rootfs_size_bytes": 1}),
            bundle_formats.ROOTFS_FORMAT_EXT4,
        )

    def test_a_recorded_format_is_read_back(self):
        for fmt in bundle_formats.SUPPORTED_ROOTFS_FORMATS:
            with self.subTest(fmt=fmt):
                self.assertEqual(
                    bundle_formats.rootfs_format_of({"rootfs_format": fmt}), fmt
                )

    def test_only_squashfs_and_erofs_are_read_only(self):
        self.assertTrue(bundle_formats.is_read_only_format("squashfs"))
        self.assertTrue(bundle_formats.is_read_only_format("erofs"))
        self.assertFalse(bundle_formats.is_read_only_format("ext4"))

    def test_each_format_has_its_own_image_name(self):
        # Two images in one bundle directory claiming to be the rootfs is how a
        # rebuild in another format would come to boot the stale one.
        names = list(bundle_formats.ROOTFS_IMAGE_NAMES.values())
        self.assertEqual(len(names), len(set(names)))
        # ext4 keeps its historical name, which the runtime copy and the prune
        # sweep look for.
        self.assertEqual(
            bundle_formats.ROOTFS_IMAGE_NAMES[bundle_formats.ROOTFS_FORMAT_EXT4],
            "rootfs.ext4",
        )


@unittest.skipIf(BUILD_IMPORT_ERROR is not None, f"Missing runtime dependencies: {BUILD_IMPORT_ERROR}")
class BundleDiscoveryTests(unittest.TestCase):
    """A read-only bundle is found, sized and never rebuilt for being small."""

    def _bundle(self, tmpdir, fmt, size, requested=None):
        bundle_dir = Path(tmpdir) / microvm_paths.FAMILY_DIR_NAME / "svc" / "x86_64"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / bundle_formats.ROOTFS_IMAGE_NAMES[fmt]).write_bytes(b"x" * size)
        with open(bundle_dir / "bundle.json", "w", encoding="utf-8") as f:
            json.dump(
                {"rootfs_format": fmt, "rootfs_size_bytes": size,
                 "requested_disk_space_bytes": requested},
                f,
            )
        return bundle_dir

    def test_an_erofs_bundle_is_recognised_as_built(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._bundle(tmpdir, bundle_formats.ROOTFS_FORMAT_EROFS, 2048)
            with patch.object(microvm_paths, "cache_root", return_value=tmpdir), \
                 patch.object(ch_build, "CACHE", tmpdir):
                self.assertTrue(ch_build.is_service_built("svc"))
                self.assertEqual(ch_build.built_rootfs_size_bytes("svc"), 2048)

    def test_a_read_only_bundle_smaller_than_its_declaration_is_not_rebuilt(self):
        # The rw rule is "rebuild if the image is smaller than the request". For a
        # ro service the image is BY CONSTRUCTION smaller than the request -- it is
        # compressed, and the request is a ceiling -- so applying that rule would
        # rebuild it on every launch, forever, to produce the same image again.
        service = _service(
            filesystem=_ro_filesystem(_complete_branch()), disk_space=1024 * MIB
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            self._bundle(
                tmpdir, bundle_formats.ROOTFS_FORMAT_EROFS, 2048, requested=1024 * MIB
            )
            with patch.object(microvm_paths, "cache_root", return_value=tmpdir), \
                 patch.object(ch_build, "CACHE", tmpdir):
                self.assertTrue(
                    ch_build._is_service_built_for_arch("svc", "x86_64", service=service)
                )

    def test_a_writable_bundle_smaller_than_its_declaration_is_still_rebuilt(self):
        # Unchanged behaviour on the path that has floors.
        service = _service(disk_space=4096)

        with tempfile.TemporaryDirectory() as tmpdir:
            self._bundle(tmpdir, bundle_formats.ROOTFS_FORMAT_EXT4, 2048, requested=4096)
            with patch.object(microvm_paths, "cache_root", return_value=tmpdir), \
                 patch.object(ch_build, "CACHE", tmpdir):
                self.assertFalse(
                    ch_build._is_service_built_for_arch("svc", "x86_64", service=service)
                )


# ------------------------------------------------------------------- point 6


@unittest.skipIf(LIMITS_IMPORT_ERROR is not None, f"Missing runtime dependencies: {LIMITS_IMPORT_ERROR}")
class ReadOnlySizingTests(unittest.TestCase):
    """The floors are skipped, and `disk_space` becomes a ceiling."""

    def test_a_read_only_image_is_sized_at_its_tree(self):
        # No MIN_ROOTFS_BYTES, no OVERHEAD_BYTES. This is the 64 MiB the issue
        # measured, and the 128 MiB floor under it.
        service = _service(filesystem=_ro_filesystem(_complete_branch()))
        self.assertEqual(
            ch_limits.initial_rootfs_size_bytes(
                service=service, total_bytes=12 * MIB, read_only=True
            ),
            12 * MIB,
        )

    def test_a_writable_image_keeps_every_floor(self):
        service = _service()
        self.assertEqual(
            ch_limits.initial_rootfs_size_bytes(service=service, total_bytes=12 * MIB),
            ch_limits.MIN_ROOTFS_BYTES,
        )
        self.assertEqual(
            ch_limits.initial_rootfs_size_bytes(service=service, total_bytes=200 * MIB),
            200 * MIB + ch_limits.OVERHEAD_BYTES,
        )

    def test_a_declared_figure_is_not_a_floor_for_a_read_only_image(self):
        # The pdf capsule from the issue: 1 GiB declared, 221 MiB of content, and
        # 1 GiB of ext4 actually built and billed.
        service = _service(
            filesystem=_ro_filesystem(_complete_branch()), disk_space=1024 * MIB
        )
        self.assertEqual(
            ch_limits.initial_rootfs_size_bytes(
                service=service, total_bytes=221 * MIB, read_only=True
            ),
            221 * MIB,
        )

    def test_a_tree_above_the_declared_ceiling_is_refused(self):
        service = _service(
            filesystem=_ro_filesystem(_complete_branch()), disk_space=100 * MIB
        )
        with self.assertRaisesRegex(ValueError, "ceiling, not a floor"):
            ch_limits.assert_within_disk_space_ceiling(
                service=service, total_bytes=221 * MIB
            )

    def test_a_tree_within_the_ceiling_passes(self):
        service = _service(
            filesystem=_ro_filesystem(_complete_branch()), disk_space=1024 * MIB
        )
        ch_limits.assert_within_disk_space_ceiling(
            service=service, total_bytes=221 * MIB
        )

    def test_a_service_declaring_no_disk_declares_no_ceiling(self):
        service = _service(filesystem=_ro_filesystem(_complete_branch()))
        ch_limits.assert_within_disk_space_ceiling(service=service, total_bytes=9 * MIB)

    def test_a_read_only_instance_is_priced_at_its_image(self):
        billable = ch_limits.billable_resources(
            celaut.Sysresources(disk_space=1024 * MIB),
            built_rootfs_size_bytes=80 * MIB,
            read_only=True,
        )
        self.assertEqual(billable.disk_space, 80 * MIB)

    def test_a_writable_instance_is_priced_at_the_floor_as_before(self):
        billable = ch_limits.billable_resources(
            celaut.Sysresources(disk_space=10 * MIB),
            built_rootfs_size_bytes=12 * MIB,
        )
        self.assertEqual(billable.disk_space, ch_limits.MIN_ROOTFS_BYTES)

    def test_pricing_without_a_built_image_keeps_the_floor(self):
        # read_only alone is not enough: the image has to exist for its size to be
        # the answer, and quoting 0 disk would bill the instance for nothing.
        billable = ch_limits.billable_resources(
            celaut.Sysresources(disk_space=10 * MIB), read_only=True
        )
        self.assertEqual(billable.disk_space, ch_limits.MIN_ROOTFS_BYTES)

    def test_is_read_only_service_reads_an_inline_filesystem(self):
        self.assertTrue(
            ch_limits.is_read_only_service(
                _service(filesystem=_ro_filesystem(_complete_branch()))
            )
        )
        self.assertFalse(ch_limits.is_read_only_service(_service()))

    def test_a_service_with_no_filesystem_prices_as_writable(self):
        # Including one whose tree is stored as a block: limits.py does not read
        # the block registry, so it answers "rw" -- which over-prices rather than
        # under-prices, and only over-pricing is recoverable.
        self.assertFalse(ch_limits.is_read_only_service(celaut.Service()))

    def test_an_unreadable_read_mode_does_not_make_pricing_refuse_a_manifest(self):
        # The build raises on it, with the path and the accepted values. Pricing
        # is not where a manifest is rejected.
        fs = celaut.Service.Container.Filesystem()
        fs.xattrs["read_mode"] = b"garbage"
        self.assertFalse(ch_limits.is_read_only_service(_service(filesystem=fs)))


# ------------------------------------------------------------------- point 5


@unittest.skipIf(EXECUTE_IMPORT_ERROR is not None, f"Missing runtime dependencies: {EXECUTE_IMPORT_ERROR}")
class BootPathTests(unittest.TestCase):
    """The cmdline carries the access mode and the filesystem type, for both VMMs."""

    def _ch_cmdline(self, fmt):
        with patch.object(ch_execute.network, "guest_ip_cmdline_token", return_value="ip=x"), \
             patch.object(ch_execute.microvm_guest, "serial_device", return_value="ttyS0"), \
             patch.object(ch_execute, "KERNEL_CMDLINE_EXTRA", ""):
            return ch_execute._kernel_cmdline(
                vm_ip="10.0.0.2", netmask="255.255.255.0", rootfs_format=fmt
            )

    def test_an_ext4_bundle_still_boots_rw(self):
        cmdline = self._ch_cmdline("ext4")
        self.assertIn(" rw ", f" {cmdline} ")
        self.assertIn("rootfstype=ext4", cmdline)
        self.assertNotIn(" ro ", f" {cmdline} ")

    def test_a_read_only_bundle_boots_ro_with_its_own_type(self):
        for fmt in ("squashfs", "erofs"):
            with self.subTest(fmt=fmt):
                cmdline = self._ch_cmdline(fmt)
                self.assertIn(" ro ", f" {cmdline} ")
                self.assertIn(f"rootfstype={fmt}", cmdline)
                self.assertNotIn(" rw ", f" {cmdline} ")

    def test_the_cmdline_defaults_to_ext4_rw(self):
        # Called without the argument at all -- the shape every existing caller
        # and every existing bundle produces.
        self.assertIn("rootfstype=ext4", self._ch_cmdline(bundle_formats.ROOTFS_FORMAT_EXT4))

    def test_qemu_builds_the_same_cmdline(self):
        # QEMU boots the bundles the microVM builder wrote, including read-only
        # ones, and the /init reading these tokens is the same /init. The two
        # backends disagreeing here is a guest that fails inside the initramfs.
        with patch.object(qemu_execute.network, "guest_ip_cmdline_token", return_value="ip=x"):
            ro = qemu_execute.build_kernel_cmdline(
                arch="linux/amd64", vm_ip="10.0.0.2", netmask="255.255.255.0",
                rootfs_format="erofs",
            )
            rw = qemu_execute.build_kernel_cmdline(
                arch="linux/amd64", vm_ip="10.0.0.2", netmask="255.255.255.0",
            )

        self.assertIn("rootfstype=erofs", ro)
        self.assertIn(" ro ", f" {ro} ")
        self.assertIn("rootfstype=ext4", rw)
        self.assertIn(" rw ", f" {rw} ")


class InitramfsReadOnlyMountTests(unittest.TestCase):
    """/init's half of the contract, read out of the builder script.

    Stdlib-only, like `test_ch_initramfs_builder.py`: the script is the artifact,
    and the assertions are about what it will do rather than about running it.
    """

    def setUp(self):
        self.content = Path("bash/build_ch_initramfs.sh").read_text(encoding="utf-8")

    def test_init_no_longer_hardcodes_an_ext4_rw_mount(self):
        self.assertNotIn("mount -t ext4 -o rw /dev/vda", self.content)

    def test_init_reads_the_type_and_access_off_the_cmdline(self):
        self.assertIn("rootfstype=*)", self.content)
        self.assertIn('mount -t "$ROOTFSTYPE" -o', self.content)

    def test_init_defaults_to_ext4_rw_when_the_cmdline_says_nothing(self):
        # A v2 initramfs booted on an old cmdline must behave exactly as v1 did.
        self.assertIn("ROOTFSTYPE=ext4", self.content)
        self.assertIn("ROOTACCESS=rw", self.content)

    def test_init_refuses_a_filesystem_it_was_not_built_for(self):
        self.assertIn("ext4|squashfs|erofs)", self.content)
        self.assertIn("unsupported rootfstype", self.content)

    def test_a_read_only_root_gets_tmpfs_on_tmp_and_run(self):
        # /tmp is the one thing the issue grants an immutable service; without it
        # a ro guest gets EROFS on its first write and dies as PID 1.
        self.assertIn("mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /newroot/tmp", self.content)
        self.assertIn("mount -t tmpfs -o mode=755,nosuid,nodev tmpfs /newroot/run", self.content)

    def test_a_read_only_root_is_overlaid_so_metadata_can_reach_the_guest(self):
        self.assertIn("mount -t overlay overlay", self.content)
        self.assertIn("lowerdir=/lower,upperdir=/overlay/upper,workdir=/overlay/work", self.content)

    def test_the_metadata_device_is_mounted_and_then_unmounted(self):
        # The service must never see it: it is the node's carrier, not part of the
        # filesystem the service published.
        self.assertIn("/dev/vdb", self.content)
        self.assertIn("umount /meta", self.content)

    def test_the_applets_init_now_calls_are_declared(self):
        # bash/guest-kernel/applets.txt is the single source of truth, and an
        # undeclared applet is a symlink to nothing and a guest that dies at boot.
        applets = Path("bash/guest-kernel/applets.txt").read_text(encoding="utf-8")
        declared = {
            line.strip()
            for line in applets.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        self.assertIn("cp", declared)
        self.assertIn("umount", declared)

    def test_the_contract_version_was_bumped_with_init(self):
        # /init's contract with execute.py changed in both directions (it reads
        # new cmdline tokens, and it needs a /dev/vdb this checkout attaches), so
        # a pinned v1 asset must not silently outlive it. Asserting "moved past
        # v1" rather than pinning the exact current version keeps this regression
        # test from going stale at every later, unrelated contract bump (#405
        # moved it again, to v3, for a change /init's v2 cmdline handling has
        # nothing to do with).
        from src.virtualizers.microvm import initramfs as microvm_initramfs

        self.assertNotEqual(microvm_initramfs.CONTRACT_VERSION, "v1")
        self.assertIn(
            f"nodo-ch-initramfs:{microvm_initramfs.CONTRACT_VERSION}", self.content
        )


class GuestKernelSupportsReadOnlyRootfsTests(unittest.TestCase):
    """The shipped kernel can actually mount what the builder now produces.

    Without these the node would build a `ro` image, write a `rootfstype=erofs`
    cmdline, and hand the guest a kernel with no such filesystem -- a mount
    failure inside the initramfs, which is the failure mode with the least
    evidence attached. NOTE: this means the guest kernel has to be REBUILT and
    republished; a node running a pre-#369 pinned kernel cannot boot a ro image.
    """

    def setUp(self):
        self.config = Path("bash/guest-kernel/nodo-guest.config").read_text(encoding="utf-8")

    def test_squashfs_and_erofs_are_enabled(self):
        self.assertIn("CONFIG_SQUASHFS=y", self.config)
        self.assertIn("CONFIG_EROFS_FS=y", self.config)

    def test_squashfs_is_no_longer_explicitly_disabled(self):
        self.assertNotIn("# CONFIG_SQUASHFS is not set", self.config)

    def test_the_decompressors_the_build_tools_default_to_are_present(self):
        # mkfs.erofs -zlz4hc, and squashfs's own default of zlib.
        self.assertIn("CONFIG_EROFS_FS_ZIP=y", self.config)
        self.assertIn("CONFIG_SQUASHFS_ZLIB=y", self.config)

    def test_overlayfs_is_available_for_the_read_only_metadata_overlay(self):
        self.assertIn("CONFIG_OVERLAY_FS=y", self.config)

    def test_the_kernel_build_asserts_both_filesystems_survived_kconfig(self):
        # A fragment entry is a request: Kconfig drops a symbol whose dependencies
        # are unmet, silently. The build script fails instead.
        build = Path("bash/guest-kernel/build.sh").read_text(encoding="utf-8")
        self.assertIn("CONFIG_SQUASHFS", build)
        self.assertIn("CONFIG_EROFS_FS", build)


if __name__ == "__main__":
    unittest.main()
