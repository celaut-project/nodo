import unittest
from pathlib import Path


class SetupScriptsNoQemuTests(unittest.TestCase):
    def test_x86_setup_has_no_qemu_or_binfmt_steps(self):
        content = Path("bash/setup_linux_x86.sh").read_text(encoding="utf-8").lower()
        self.assertNotIn("qemu", content)
        self.assertNotIn("binfmt", content)
        self.assertNotIn("multiarch/qemu-user-static", content)
        self.assertNotIn("install_portable_jre", content)

    def test_arm_setup_has_no_qemu_or_binfmt_steps(self):
        content = Path("bash/setup_linux_arm.sh").read_text(encoding="utf-8").lower()
        self.assertNotIn("qemu", content)
        self.assertNotIn("binfmt", content)
        self.assertNotIn("multiarch/qemu-user-static", content)
        self.assertNotIn("install_portable_jre", content)

    def test_generic_java_installer_exists(self):
        content = Path("bash/install_java.sh").read_text(encoding="utf-8")
        self.assertIn("uname -m", content)
        self.assertIn("Temurin JRE", content)
        self.assertIn(".dependencies.java.RUNTIME_ROOT", content)


if __name__ == "__main__":
    unittest.main()
