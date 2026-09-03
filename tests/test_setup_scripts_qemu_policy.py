"""What kind of QEMU the installer may bring in, and what it must never bring in.

Nodo emulates a foreign architecture by booting a whole guest under
``qemu-system-<arch>`` -- the same rootfs, kernel and initramfs Cloud Hypervisor
boots natively, just with ``-accel tcg``. It must NOT do it with ``binfmt_misc``
plus ``qemu-user-static``, which registers a host-wide interpreter for foreign
binaries: that leaks foreign execution into the host, outside any VM boundary,
and every service the node runs is untrusted code.

So "no qemu at all" is the wrong invariant (the installer now provisions the
foreign emulator on purpose); "no process-level emulation on the host" is the
right one.
"""
import unittest
from pathlib import Path

SETUP_SCRIPTS = ("bash/setup_linux_x86.sh", "bash/setup_linux_arm.sh")


class SetupScriptsQemuPolicyTests(unittest.TestCase):
    @staticmethod
    def _code_lines(path):
        # Comments explain why binfmt is *not* used, so only real code is checked.
        for line in Path(path).read_text(encoding="utf-8").lower().splitlines():
            if line.strip().startswith("#"):
                continue
            yield line

    def test_setup_never_installs_host_level_binary_emulation(self):
        for script in SETUP_SCRIPTS:
            for line in self._code_lines(script):
                with self.subTest(script=script, line=line):
                    self.assertNotIn("binfmt", line)
                    self.assertNotIn("qemu-user", line)

    def test_setup_installs_the_system_emulator_for_the_foreign_arch(self):
        for script in SETUP_SCRIPTS:
            with self.subTest(script=script):
                content = Path(script).read_text(encoding="utf-8")
                self.assertIn('install_foreign_arch_emulator "$FOREIGN_ARCH_TAG"', content)

    def test_only_system_emulator_packages_are_named(self):
        # The package names live in lib_pkg.sh; any qemu package the installer can
        # reach must be a qemu-system one.
        content = Path("bash/lib_pkg.sh").read_text(encoding="utf-8")
        for line in content.splitlines():
            if "qemu" not in line.lower() or line.strip().startswith("#"):
                continue
            self.assertNotIn("qemu-user", line)
            self.assertNotIn("binfmt", line)

    def test_setup_does_not_install_a_portable_jre(self):
        for script in SETUP_SCRIPTS:
            with self.subTest(script=script):
                content = Path(script).read_text(encoding="utf-8")
                self.assertNotIn("install_portable_jre", content)

    def test_generic_java_installer_exists(self):
        content = Path("bash/install_java.sh").read_text(encoding="utf-8")
        self.assertIn("uname -m", content)
        self.assertIn("Temurin JRE", content)
        self.assertIn(".dependencies.java.RUNTIME_ROOT", content)


if __name__ == "__main__":
    unittest.main()
