import re
import unittest
from pathlib import Path


ARM_SETUP = Path("bash/setup_linux_arm.sh")
X86_SETUP = Path("bash/setup_linux_x86.sh")
LIB_PKG = Path("bash/lib_pkg.sh")


class PackageManagerAbstractionTests(unittest.TestCase):
    def test_setup_scripts_do_not_call_a_package_manager_directly(self):
        # install.sh must work on any Linux, so the setup scripts talk to
        # bash/lib_pkg.sh instead of hardcoding apt/dnf. The single exception is
        # the Debian-only systemd install for the WSL image, which is guarded by
        # PKG_MGR and asserted separately below.
        for script in (ARM_SETUP, X86_SETUP):
            with self.subTest(script=str(script)):
                content = script.read_text(encoding="utf-8")
                self.assertIn('. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib_pkg.sh"', content)
                self.assertIn("detect_pkg_mgr", content)
                self.assertIn("pkg_install_host_dependencies", content)
                self.assertNotIn("apt-cache", content)
                self.assertNotIn("dpkg", content)
                self.assertNotIn("locale-gen", content)

        self.assertNotIn("apt-get", ARM_SETUP.read_text(encoding="utf-8"))

        x86 = X86_SETUP.read_text(encoding="utf-8")
        self.assertLess(
            x86.index('if [ "$PKG_MGR" = "apt" ]; then'),
            x86.index("apt-get install -y --no-install-recommends systemd"),
        )

    def test_every_dependency_resolves_on_apt_and_dnf(self):
        content = LIB_PKG.read_text(encoding="utf-8")

        aliases = content.split("NODO_HOST_PACKAGE_ALIASES=(")[1].split(")")[0].split()
        self.assertIn("compiler", aliases)
        self.assertIn("clang", aliases)

        mapping = content.split("pkg_for() {")[1].split("\n}")[0]
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assertRegex(mapping, rf"(^|\s|\|){re.escape(alias)}(\)|\|)")

    def test_unknown_package_manager_fails_with_a_manual_install_hint(self):
        content = LIB_PKG.read_text(encoding="utf-8")
        self.assertIn("No supported package manager found", content)
        self.assertIn("docs/INSTALL.md", content)

    def test_busybox_is_not_a_host_dependency(self):
        # The guest's busybox comes from the Nodo release, so the host does not need
        # one — that is the whole point of shipping it: distros compile different
        # applet sets, and the guest userspace must not depend on which.
        lib = LIB_PKG.read_text(encoding="utf-8")
        aliases = lib.split("NODO_HOST_PACKAGE_ALIASES=(")[1].split(")")[0].split()
        self.assertNotIn("busybox", aliases)

        for script in (ARM_SETUP, X86_SETUP):
            with self.subTest(script=str(script)):
                content = script.read_text(encoding="utf-8")
                self.assertIn('download_guest_asset "busybox-${CH_ARCH_TAG//\\//-}"', content)


class ServiceUnitPortabilityTests(unittest.TestCase):
    def test_unit_group_is_not_hardcoded(self):
        # `sudo` exists on Debian, `wheel` on Fedora/RHEL; systemd fails to start a
        # unit whose Group cannot be resolved.
        template = Path("bash/nodo.service.template").read_text(encoding="utf-8")
        self.assertIn("Group={{ADMIN_GROUP}}", template)
        self.assertNotIn("Group=sudo", template)

    def test_installer_and_doctor_resolve_the_group_the_same_way(self):
        # doctor rewrites the unit whenever its rendering differs from the installed
        # one, so a mismatch here silently stops the service on every `nodo doctor`.
        installer = Path("install.sh").read_text(encoding="utf-8")
        doctor = Path("src/commands/doctor.py").read_text(encoding="utf-8")

        self.assertIn("for group in sudo wheel; do", installer)
        self.assertIn('-e "s|{{ADMIN_GROUP}}|$ADMIN_GROUP|g"', installer)
        self.assertIn('for name in ("sudo", "wheel"):', doctor)
        self.assertIn('"{{ADMIN_GROUP}}": _resolve_admin_group()', doctor)

    def test_doctor_renders_the_template_without_placeholders(self):
        import sys

        sys.path.insert(0, ".")
        from src.commands.doctor import _render_service_template

        rendered = _render_service_template(
            Path("bash/nodo.service.template").read_text(encoding="utf-8"), "/nodo"
        )
        self.assertNotIn("{{", rendered)
        group = [l for l in rendered.splitlines() if l.startswith("Group=")][0]
        self.assertIn(group.split("=", 1)[1], ("sudo", "wheel", "root"))


class HostArchitectureGatingTests(unittest.TestCase):
    def test_setup_disables_the_architecture_the_host_cannot_boot(self):
        # This profile installs no QEMU/binfmt, so a node must not accept services
        # built for the other architecture: it could never boot them.
        arm = ARM_SETUP.read_text(encoding="utf-8")
        x86 = X86_SETUP.read_text(encoding="utf-8")

        self.assertIn(".builder.X86_SUPPORT = false", arm)
        self.assertIn(".packer.X86_PACKER_SUPPORT = false", arm)
        self.assertIn(".builder.ARM_SUPPORT = false", x86)
        self.assertIn(".packer.ARM_PACKER_SUPPORT = false", x86)


class SourceBuildToolchainTests(unittest.TestCase):
    def test_pip_falls_back_to_gcc_when_clang_is_absent(self):
        # psutil has no linux-aarch64 wheel, and the portable CPython's sysconfig
        # records CC=clang: on a host without clang, pip dies with
        # "No such file or directory: 'clang'".
        for script in (ARM_SETUP, X86_SETUP):
            with self.subTest(script=str(script)):
                content = script.read_text(encoding="utf-8")
                self.assertIn('if ! command -v clang >/dev/null 2>&1; then', content)
                self.assertIn('export CC="${CC:-gcc}" CXX="${CXX:-g++}"', content)


if __name__ == "__main__":
    unittest.main()
