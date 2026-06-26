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
