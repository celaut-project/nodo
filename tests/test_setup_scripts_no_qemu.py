import unittest
from pathlib import Path


class SetupScriptsNoQemuTests(unittest.TestCase):
    def test_x86_setup_has_no_qemu_or_binfmt_steps(self):
        content = Path("bash/setup_ubuntu_x86.sh").read_text(encoding="utf-8").lower()
        self.assertNotIn("qemu", content)
        self.assertNotIn("binfmt", content)
        self.assertNotIn("multiarch/qemu-user-static", content)

    def test_arm_setup_has_no_qemu_or_binfmt_steps(self):
        content = Path("bash/setup_ubuntu_arm.sh").read_text(encoding="utf-8").lower()
        self.assertNotIn("qemu", content)
        self.assertNotIn("binfmt", content)
        self.assertNotIn("multiarch/qemu-user-static", content)


if __name__ == "__main__":
    unittest.main()
