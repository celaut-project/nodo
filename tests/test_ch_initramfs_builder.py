import unittest
from pathlib import Path


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

                self.assertIn("download_guest_kernel \"$ch_kernel_target\"", content)
                self.assertIn("${GUEST_KERNEL_VERSION}", content)
                self.assertIn("Guest kernel SHA256 mismatch", content)
                self.assertNotIn("/boot/vmlinuz", content)
                self.assertNotIn("resolve_boot_asset", content)
                self.assertIn(
                    '"$ch_initramfs_builder" "$TARGET_DIR" "$CH_ARCH_TAG" "$ch_initramfs_target"',
                    content,
                )

    def test_installer_pins_a_guest_kernel_release(self):
        content = Path("install.sh").read_text(encoding="utf-8")

        self.assertIn('GUEST_KERNEL_VERSION="guest-kernel-', content)
        self.assertIn(
            '"$SETUP_SCRIPT" "$TARGET_DIR" "$CH_VERSION" "$GUEST_KERNEL_VERSION"',
            content,
        )


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
