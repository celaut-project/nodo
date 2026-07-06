import unittest
from pathlib import Path


class CloudHypervisorInitramfsBuilderTests(unittest.TestCase):
    def test_builder_includes_and_loads_virtio_modules_before_vda_wait(self):
        content = Path("bash/build_ch_initramfs.sh").read_text(encoding="utf-8")

        self.assertIn("modprobe --set-version \"$kernel_release\" --show-depends", content)
        self.assertIn("virtio_blk", content)
        self.assertIn("virtio_net", content)
        self.assertIn("etc/nodo-virtio-modules.list", content)
        self.assertIn("insmod \"$module_path\"", content)
        self.assertLess(
            content.index("insmod \"$module_path\""),
            content.index("while [ \"$i\" -lt \"$WAIT_SECONDS\" ]"),
        )

    def test_builder_strips_trailing_whitespace_from_module_path(self):
        # modprobe --show-depends on Ubuntu 22.04 appends a trailing space after the
        # .ko path; the builder must trim it before the [ -f ] existence check, or the
        # install aborts with "modprobe returned missing module path" on a file that exists.
        content = Path("bash/build_ch_initramfs.sh").read_text(encoding="utf-8")
        self.assertIn(
            'source_path="${source_path%"${source_path##*[![:space:]]}"}"',
            content,
        )
        self.assertLess(
            content.index('source_path="${source_path%'),
            content.index('|| fail "modprobe returned missing module path'),
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

    def test_setup_scripts_pass_guest_kernel_path_to_initramfs_builder(self):
        for script in ("bash/setup_ubuntu_x86.sh", "bash/setup_ubuntu_arm.sh"):
            with self.subTest(script=script):
                content = Path(script).read_text(encoding="utf-8")
                self.assertIn(
                    '"$ch_initramfs_builder" "$TARGET_DIR" "$CH_ARCH_TAG" "$ch_initramfs_target" "$kernel_source"',
                    content,
                )


if __name__ == "__main__":
    unittest.main()
