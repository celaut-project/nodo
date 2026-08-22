import gzip
import subprocess
import tempfile
import unittest
from pathlib import Path

# src.virtualizers.ch.initramfs is stdlib-only by design, so it imports on a bare
# checkout -- which is why these tests run here instead of alongside execute.py's,
# where the whole module is skipped when grpc is absent.
from src.virtualizers.ch import initramfs as ch_initramfs


class CloudHypervisorInitramfsBuilderTests(unittest.TestCase):
    def test_initramfs_loads_no_kernel_modules(self):
        # The guest kernel is built by bash/guest-kernel/ with CONFIG_MODULES off and
        # every driver a service needs compiled in, so the initramfs has nothing to
        # discover, copy or insmod. Keeping any of that machinery around would
        # re-introduce a dependency on the *host's* /lib/modules, which is what made
        # the initramfs distro-specific in the first place.
        content = Path("bash/build_ch_initramfs.sh").read_text(encoding="utf-8")

        self.assertNotIn("modprobe", content)
        self.assertNotIn("insmod", content)
        self.assertNotIn("nodo-virtio-modules.list", content)
        self.assertNotIn(".ko", content)

    def test_builder_takes_no_kernel_path_argument(self):
        content = Path("bash/build_ch_initramfs.sh").read_text(encoding="utf-8")

        self.assertIn("fail \"Usage: $0 <TARGET_DIR> <ARCH_TAG> <OUTPUT_PATH>\"", content)
        self.assertNotIn("KERNEL_PATH", content)

    def test_builder_verifies_the_applets_init_needs(self):
        # busybox is the last input to the initramfs that comes from the host, and
        # distros compile different applet sets. Symlinking without checking defers
        # the failure to guest boot ("applet not found"), on some distros only.
        content = Path("bash/build_ch_initramfs.sh").read_text(encoding="utf-8")

        self.assertIn("guest-kernel/applets.txt", content)
        self.assertIn("lacks applets required by the guest init", content)
        self.assertLess(
            content.index("missing_applets"),
            content.index('for applet in "${BUSYBOX_APPLETS[@]}"; do\n    ln -sf'),
        )

    def test_builder_mounts_cgroup2_after_sys_move(self):
        # The guest has no init system (the entrypoint is PID 1 out of switch_root),
        # so the initramfs must mount the unified cgroup-v2 hierarchy itself; otherwise
        # container-runtime services (rootful dockerd) abort with "Devices cgroup isn't
        # mounted" and the guest kernel panics. Must come AFTER /sys is moved to newroot
        # (the cgroup fs lives under /newroot/sys/fs/cgroup).
        content = Path("bash/build_ch_initramfs.sh").read_text(encoding="utf-8")
        self.assertIn("mount -t cgroup2 none /newroot/sys/fs/cgroup", content)
        self.assertLess(
            content.index('mount --move /sys /newroot/sys'),
            content.index("mount -t cgroup2 none /newroot/sys/fs/cgroup"),
        )

    def test_setup_scripts_download_the_pinned_guest_kernel(self):
        # Never the host's /boot kernel: distro kernels differ in size and format, and
        # Fedora/RHEL ship a CONFIG_EFI_ZBOOT image that Cloud Hypervisor's PE loader
        # rejects outright (UefiTooBig).
        for script in ("bash/setup_linux_x86.sh", "bash/setup_linux_arm.sh"):
            with self.subTest(script=script):
                content = Path(script).read_text(encoding="utf-8")

                self.assertIn('download_guest_asset "vmlinuz-${CH_ARCH_TAG//\\//-}"', content)
                self.assertIn("${GUEST_KERNEL_VERSION}", content)
                self.assertIn("SHA256 mismatch for", content)
                self.assertNotIn("/boot/vmlinuz", content)
                self.assertNotIn("resolve_boot_asset", content)

                # The initramfs is a release asset as well, for the same reason as
                # the kernel: assembling it here would make the guest a function of
                # whatever cpio, gzip, busybox and umask the host happens to have.
                self.assertIn(
                    'download_guest_asset "initramfs-${CH_ARCH_TAG//\\//-}" "$ch_initramfs_target" 0644',
                    content,
                )
                self.assertNotIn("ch_initramfs_builder", content)

    def test_installer_pins_a_guest_kernel_release(self):
        # There are no versioned guest tags: one mutable `guest-kernel` release, made
        # a content reference by the digests committed in SHA256SUMS.pinned. This
        # asserted a `guest-kernel-vN` scheme that was never published (only ever
        # local v1/v2 tags), so it failed against the tag actually in use.
        content = Path("install.sh").read_text(encoding="utf-8")

        self.assertIn('GUEST_KERNEL_VERSION="guest-kernel"', content)
        self.assertIn(
            '"$SETUP_SCRIPT" "$TARGET_DIR" "$CH_VERSION" "$GUEST_KERNEL_VERSION"',
            content,
        )

        # The tag alone is not the pin; the committed digests are.
        pin = Path("bash/guest-kernel/SHA256SUMS.pinned").read_text(encoding="utf-8")
        self.assertIn("TAG guest-kernel", pin)


class GuestUserspaceTests(unittest.TestCase):
    def test_applet_list_is_the_single_source_of_truth(self):
        # The builder symlinks these into the initramfs and build-busybox.sh asserts
        # the binary provides them. Two lists would drift, and the drift only shows
        # up as a guest that boots without a working /init.
        applets = Path("bash/guest-kernel/applets.txt").read_text(encoding="utf-8")
        names = [l for l in applets.splitlines() if l and not l.startswith("#")]
        self.assertIn("ip", names)
        self.assertIn("switch_root", names)

        for consumer in ("bash/build_ch_initramfs.sh", "bash/guest-kernel/build-busybox.sh"):
            with self.subTest(consumer=consumer):
                self.assertIn("applets.txt", Path(consumer).read_text(encoding="utf-8"))

    def test_shipped_busybox_must_be_static(self):
        # The initramfs has no libc and no loader: a dynamically linked busybox
        # cannot start, and the guest dies before /init runs.
        build = Path("bash/guest-kernel/build-busybox.sh").read_text(encoding="utf-8")
        self.assertIn("CONFIG_STATIC=y", build)
        self.assertIn("not a dynamic executable", build)
        self.assertIn("cannot link statically", build)

    def test_initramfs_requires_the_provisioned_busybox(self):
        # Silently falling back to the host's busybox is how the guest used to differ
        # per node. The fallback survives only as an explicit dev opt-in, so an
        # installer that failed to provision the asset stops instead of shipping a
        # guest nobody else is running.
        content = Path("bash/build_ch_initramfs.sh").read_text(encoding="utf-8")
        self.assertIn('PROVISIONED_BUSYBOX="$TARGET_DIR/cloud_hypervisor/busybox/${ARCH_TAG}/busybox"', content)
        self.assertIn('elif [ "${NODO_ALLOW_HOST_BUSYBOX:-0}" = "1" ]; then', content)
        self.assertIn("No provisioned busybox at", content)
        self.assertLess(
            content.index("PROVISIONED_BUSYBOX"),
            content.index('BUSYBOX_BIN="$(command -v busybox || true)"'),
        )

    def test_initramfs_build_is_byte_reproducible(self):
        # The published asset is only auditable if rebuilding this commit reproduces
        # it exactly. newc records mode, mtime, uid, gid and inode per entry, and the
        # tree is staged in a fresh mktemp dir, so every one of these is load-bearing.
        content = Path("bash/build_ch_initramfs.sh").read_text(encoding="utf-8")

        self.assertIn("-type d -exec chmod 0755", content)
        self.assertIn("-type f -exec chmod 0644", content)
        self.assertIn("touch -h -d @0", content)
        self.assertIn("--reproducible", content)
        self.assertIn("--owner 0:0", content)
        self.assertIn("gzip -9n", content)

    def test_nothing_reads_the_initramfs_with_a_distro_specific_tool(self):
        # The gzip'd newc cpio layout is a kernel ABI, but each distro brands its own
        # inspector for it (initramfs-tools ships lsinitramfs, dracut lsinitrd). A
        # launch that required one failed outright on Fedora, and the builder's own
        # self-check silently skipped itself there.
        for path in (
            "bash/build_ch_initramfs.sh",
            "src/virtualizers/ch/execute.py",
            "src/commands/doctor.py",
        ):
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                for line in content.splitlines():
                    if line.lstrip().startswith(("#", "//")):
                        continue  # comments explain why these are avoided
                    self.assertNotIn("lsinitramfs", line)
                    self.assertNotIn("lsinitrd", line)
                    self.assertNotIn("lsinitcpio", line)

    def test_the_asset_builder_proves_reproducibility_before_publishing(self):
        # Both workflows go through this one script, so the reproducibility gate
        # cannot be skipped by whichever path publishes the image.
        builder = Path("bash/guest-kernel/build-initramfs.sh").read_text(encoding="utf-8")

        self.assertIn('bash "$BUILDER"', builder)
        self.assertIn("initramfs build is not reproducible", builder)
        self.assertIn("sha256sum", builder)

    def test_ci_publishes_the_initramfs_with_the_rest_of_the_guest(self):
        workflow = Path(".github/workflows/guest-kernel.yml").read_text(encoding="utf-8")

        self.assertIn("bash bash/guest-kernel/build-initramfs.sh", workflow)
        self.assertIn("artifacts/initramfs-linux-arm64", workflow)
        self.assertIn("artifacts/initramfs-linux-amd64", workflow)
        # Six assets now, not four: a wrong count is how an arch silently not
        # building would otherwise reach a release.
        self.assertIn('test "$(wc -l < SHA256SUMS)" -eq 6', workflow)

        # One unversioned, mutable tag, so publishing must update in place: `create`
        # alone would fail on every run after the first. What makes the mutable tag
        # safe is the in-tree digest pin, not the tag.
        self.assertIn('- "guest-kernel"', workflow)
        self.assertNotIn("guest-kernel-v*", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("gh release upload", workflow)
        self.assertIn("--clobber", workflow)

    def test_the_initramfs_can_be_republished_onto_an_existing_release(self):
        # The initramfs changes at code speed (/init is a contract with execute.py),
        # so it must be replaceable without a ~30 minute kernel rebuild and without
        # disturbing the assets already pinned against that tag.
        workflow = Path(".github/workflows/guest-initramfs.yml").read_text(encoding="utf-8")

        self.assertIn("bash bash/guest-kernel/build-initramfs.sh", workflow)
        self.assertIn("--clobber", workflow)
        self.assertNotIn("build-busybox.sh", workflow)
        self.assertNotIn("guest-kernel/build.sh", workflow)

        # The busybox inside the image is the one this commit pins, verified against
        # the in-tree digest -- not rebuilt, and not trusted from the release's own
        # SHA256SUMS, which sits in the same mutable place as the artifact.
        self.assertIn("SHA256SUMS.pinned", workflow)
        self.assertIn("SHA256 mismatch for", workflow)
        self.assertIn("refusing to build", workflow)


class InitramfsContractTests(unittest.TestCase):
    def test_contract_version_matches_the_marker_the_builder_stamps(self):
        # execute.py refuses to launch on a version it does not speak and the builder
        # stamps it; if the two drift, every launch fails on a correctly built image.
        builder = Path("bash/build_ch_initramfs.sh").read_text(encoding="utf-8")
        self.assertIn(
            f"'{ch_initramfs.MARKER_KEY}:{ch_initramfs.CONTRACT_VERSION}", builder
        )

    def test_reads_entries_and_version_from_a_real_archive(self):
        # Reads the format bash/build_ch_initramfs.sh emits and the kernel consumes,
        # built here with cpio rather than mocked, since parsing it is the whole job.
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "root"
            (root / "bin").mkdir(parents=True)
            (root / "etc").mkdir(parents=True)
            (root / "init").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "bin" / "busybox").write_bytes(b"busybox")
            (root / ch_initramfs.MARKER_PATH).write_text(
                f"{ch_initramfs.MARKER_KEY}:{ch_initramfs.CONTRACT_VERSION}\n"
                "arch:linux/arm64\n",
                encoding="utf-8",
            )

            names = b"\0".join(
                str(p.relative_to(root)).encode() for p in sorted(root.rglob("*"))
            ) + b"\0"
            archive = subprocess.run(
                ["cpio", "--null", "-o", "--format=newc", "--quiet"],
                input=names, cwd=root, capture_output=True, check=True,
            ).stdout

            path = Path(tmp_dir) / "initramfs"
            path.write_bytes(gzip.compress(archive))

            entries, version = ch_initramfs.read(str(path))

        self.assertEqual(version, ch_initramfs.CONTRACT_VERSION)
        self.assertEqual(ch_initramfs.missing_entries(entries), [])

    def test_rejects_a_file_that_is_not_a_gzip_archive(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "initramfs"
            path.write_bytes(b"not gzip at all")
            with self.assertRaises(ch_initramfs.InitramfsReadError):
                ch_initramfs.read(str(path))

    def test_execute_and_doctor_read_through_this_one_module(self):
        # Two callers, one definition of what a nodo initramfs is. When doctor spelled
        # the required entries itself it also silently skipped the version check, so
        # `doctor` passed on an image every launch would reject.
        for path in ("src/virtualizers/ch/execute.py", "src/commands/doctor.py"):
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("initramfs as ch_initramfs", content)
                self.assertIn("ch_initramfs.CONTRACT_VERSION", content)
                self.assertIn("ch_initramfs.missing_entries", content)
                # The entry names must come from the module, not be respelled.
                self.assertNotIn('"etc/nodo-ch-initramfs.marker"', content)


class GuestKernelConfigTests(unittest.TestCase):
    def test_devices_the_initramfs_relies_on_are_built_in(self):
        # These are the drivers /init needs before any filesystem exists. With
        # CONFIG_MODULES off they can only be =y, and the build asserts it.
        fragment = Path("bash/guest-kernel/nodo-guest.config").read_text(encoding="utf-8")

        self.assertIn("# CONFIG_MODULES is not set", fragment)
        for symbol in ("CONFIG_VIRTIO_BLK", "CONFIG_VIRTIO_NET", "CONFIG_VIRTIO_FS",
                       "CONFIG_FUSE_FS", "CONFIG_OVERLAY_FS", "CONFIG_EXT4_FS"):
            self.assertIn(f"{symbol}=y", fragment)

    def test_build_fails_when_kconfig_drops_a_required_symbol(self):
        # A fragment entry is a request: Kconfig silently drops symbols whose
        # dependencies are unmet. Without these assertions the build would happily
        # ship a kernel that cannot mount a rootfs.
        build = Path("bash/guest-kernel/build.sh").read_text(encoding="utf-8")

        self.assertIn("assert_config", build)
        self.assertIn("assert_config CONFIG_MODULES n", build)
        self.assertIn("assert_config CONFIG_EFI_ZBOOT n", build)

    def test_arm64_image_magic_is_verified_before_publishing(self):
        # Cloud Hypervisor's aarch64 loader only accepts a raw arm64 Image ("ARM\\x64"
        # at offset 56). Anything else fails at boot time, on the user's machine.
        build = Path("bash/guest-kernel/build.sh").read_text(encoding="utf-8")

        self.assertIn('skip=56 count=4 status=none)" = "ARMd"', build)


if __name__ == "__main__":
    unittest.main()
