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
                self.assertIn('download_guest_asset "busybox-${asset_suffix}"', content)


SERVICE_TEMPLATE = Path("bash/nodo.service.template")


def _shell_renderers_of_the_service_template():
    """Every shell script that renders bash/nodo.service.template.

    Discovered rather than listed: the bug this guards against was adding a
    placeholder to the template and updating only some of the renderers, and a
    hardcoded list has exactly the same blind spot.
    """
    candidates = [Path("install.sh")] + sorted(Path("bash").glob("*.sh"))
    renderers = []
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        # Naming the template is not enough — lib_pkg.sh only mentions it in a
        # comment. A renderer is a script that actually substitutes into it.
        if "nodo.service.template" in text and "s|{{" in text:
            renderers.append(path)
    return renderers


class ServiceUnitPortabilityTests(unittest.TestCase):
    def test_unit_group_is_not_hardcoded(self):
        # `sudo` exists on Debian, `wheel` on Fedora/RHEL; systemd fails to start a
        # unit whose Group cannot be resolved.
        template = SERVICE_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("Group={{ADMIN_GROUP}}", template)
        self.assertNotIn("Group=sudo", template)

    def test_every_shell_renderer_substitutes_every_placeholder(self):
        # setup_linux_x86.sh once rendered the unit without {{ADMIN_GROUP}}, wrote
        # `Group={{ADMIN_GROUP}}` to /etc/systemd/system/nodo.service and aborted the
        # install on its own placeholder check — leaving a unit systemd refused to
        # load. Assert the whole placeholder set against every renderer, so adding a
        # placeholder cannot half-land again.
        placeholders = set(re.findall(r"\{\{[A-Z_]+\}\}", SERVICE_TEMPLATE.read_text(encoding="utf-8")))
        self.assertIn("{{ADMIN_GROUP}}", placeholders)

        renderers = _shell_renderers_of_the_service_template()
        self.assertIn(Path("install.sh"), renderers)
        self.assertIn(Path("bash/setup_linux_x86.sh"), renderers)

        for path in renderers:
            content = path.read_text(encoding="utf-8")
            for placeholder in sorted(placeholders):
                with self.subTest(script=str(path), placeholder=placeholder):
                    self.assertIn(f"s|{placeholder}|", content)

    def test_renderers_share_one_admin_group_resolver(self):
        # doctor rewrites the unit whenever its rendering differs from the installed
        # one, so a mismatch here silently stops the service on every `nodo doctor`.
        # The shell side must not carry its own copy of the lookup.
        lib = LIB_PKG.read_text(encoding="utf-8")
        doctor = Path("src/commands/doctor.py").read_text(encoding="utf-8")

        self.assertIn("resolve_admin_group() {", lib)
        self.assertIn("for group in sudo wheel; do", lib)
        self.assertIn('for name in ("sudo", "wheel"):', doctor)
        self.assertIn('"{{ADMIN_GROUP}}": _resolve_admin_group()', doctor)

        for path in _shell_renderers_of_the_service_template():
            with self.subTest(script=str(path)):
                content = path.read_text(encoding="utf-8")
                self.assertIn("resolve_admin_group", content)
                self.assertNotIn("for group in sudo wheel; do", content)

    def test_a_failed_render_leaves_the_installed_unit_alone(self):
        # A half-rendered unit must never reach /etc: an install that aborts should
        # leave the previous working service running, not an unloadable one.
        content = Path("bash/setup_linux_x86.sh").read_text(encoding="utf-8")
        self.assertIn('unit_tmp="$(mktemp)"', content)
        self.assertIn('install -m 0644 "$unit_tmp" "$unit_dst"', content)
        self.assertLess(
            content.index("Unresolved placeholders in rendered unit"),
            content.index('install -m 0644 "$unit_tmp" "$unit_dst"'),
        )

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
    def test_setup_restricts_only_the_packer_to_the_host_arch(self):
        # PACKING is host-only: a local build runs the target's own toolchain and
        # nodo installs no binfmt handler. EXECUTION is not: the node emulates the
        # foreign arch under QEMU, so the installer must NOT write an execution
        # gate into the config -- capability is derived from what is on disk
        # (src/utils/architectures.py), and the flags that used to declare it are
        # rejected outright (config_validation.REMOVED_KEYS).
        arm = ARM_SETUP.read_text(encoding="utf-8")
        x86 = X86_SETUP.read_text(encoding="utf-8")

        self.assertIn(".packer.X86_PACKER_SUPPORT = false", arm)
        self.assertIn(".packer.ARM_PACKER_SUPPORT = false", x86)

        for name, content in (("arm", arm), ("x86", x86)):
            with self.subTest(script=name):
                self.assertNotIn(".builder.ARM_SUPPORT", content)
                self.assertNotIn(".builder.X86_SUPPORT", content)

    def test_setup_provisions_guest_assets_for_both_architectures(self):
        # The foreign arch's kernel/initramfs are what QEMU boots, and their absence
        # is exactly how a node with `builder.ARM_SUPPORT: true` on x86_64 used to
        # fail: deep in the CH build, on a kernel path nothing had downloaded.
        for name, script, foreign in (
            ("arm", ARM_SETUP, "linux/amd64"),
            ("x86", X86_SETUP, "linux/arm64"),
        ):
            with self.subTest(script=name):
                content = script.read_text(encoding="utf-8")
                self.assertIn(f'FOREIGN_ARCH_TAG="{foreign}"', content)
                self.assertIn(
                    'provision_guest_assets_for_arch "$CH_ARCH_TAG"', content
                )
                self.assertIn(
                    'provision_guest_assets_for_arch "$FOREIGN_ARCH_TAG"', content
                )
                self.assertIn(
                    'install_foreign_arch_emulator "$FOREIGN_ARCH_TAG"', content
                )

    def test_emulator_install_never_fails_the_install(self):
        # The emulator is a large optional package: a host that cannot install it
        # must still end up with a working node that serves its own arch.
        lib = (ARM_SETUP.parent / "lib_pkg.sh").read_text(encoding="utf-8")
        body = lib.split("install_foreign_arch_emulator()", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("fail ", body)
        self.assertIn("Warning: could not install", body)


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


class PinnedGuestAssetTests(unittest.TestCase):
    """The guest is the most privileged thing nodo downloads."""

    PIN = Path("bash/guest-kernel/SHA256SUMS.pinned")
    # Every asset the setup scripts fetch, spelled out rather than derived: an
    # unpinned one is a guest nobody verified, so it is worth having to say it here
    # too. The initramfs joined the list when it stopped being built per host.
    ASSETS = (
        "busybox-linux-amd64",
        "busybox-linux-arm64",
        "initramfs-linux-amd64",
        "initramfs-linux-arm64",
        "vmlinuz-linux-amd64",
        "vmlinuz-linux-arm64",
    )

    def _pin(self):
        return self.PIN.read_text(encoding="utf-8")

    def test_pin_covers_every_asset_with_a_full_digest(self):
        digests = dict(
            (parts[1], parts[0])
            for parts in (
                line.split()
                for line in self._pin().splitlines()
                if line and not line.startswith("#") and not line.startswith("TAG")
            )
            if len(parts) == 2
        )
        self.assertEqual(sorted(digests), sorted(self.ASSETS))
        for asset, digest in digests.items():
            with self.subTest(asset=asset):
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_pin_matches_the_release_the_installer_asks_for(self):
        # A bumped GUEST_KERNEL_VERSION with stale digests must fail loudly at
        # install time rather than silently verify against the old kernel.
        installer = Path("install.sh").read_text(encoding="utf-8")
        wanted = re.search(r'GUEST_KERNEL_VERSION="([^"]+)"', installer).group(1)
        pinned = re.search(r"^TAG (\S+)$", self._pin(), re.MULTILINE).group(1)
        self.assertEqual(pinned, wanted)

    def test_setup_verifies_against_the_pin_and_not_the_release(self):
        # SHA256SUMS published next to the artifact proves only that the download was
        # not truncated: whoever can edit the release swaps both at once.
        for script in (ARM_SETUP, X86_SETUP):
            with self.subTest(script=str(script)):
                content = script.read_text(encoding="utf-8")
                self.assertIn("pinned_guest_digest", content)
                self.assertIn("SHA256SUMS.pinned", content)
                self.assertNotIn('download_file "${base_url}/SHA256SUMS"', content)

    def test_the_pinned_tag_is_checked_before_downloading(self):
        for script in (ARM_SETUP, X86_SETUP):
            with self.subTest(script=str(script)):
                content = script.read_text(encoding="utf-8")
                self.assertIn('[ "$pinned_tag" = "$GUEST_KERNEL_VERSION" ]', content)


class DownloadErrorPropagationTests(unittest.TestCase):
    def test_download_file_reports_transfer_failures(self):
        # download_file used to `return 0` unconditionally, which made every
        # `download_file ... || fail` guard in these scripts dead code.
        for script in (ARM_SETUP, X86_SETUP):
            with self.subTest(script=str(script)):
                content = script.read_text(encoding="utf-8")
                body = content.split("download_file() {")[1].split("\n}")[0]
                self.assertIn('curl -fsSL "$url" -o "$destination" || return 1', body)
                self.assertIn('wget -qO "$destination" "$url" || return 1', body)


if __name__ == "__main__":
    unittest.main()
