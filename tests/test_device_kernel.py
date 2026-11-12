"""The device variant of the guest kernel (#536, phase P5): build fragment, CI, installer."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _fragment(name):
    out = {}
    for line in (ROOT / "bash" / "guest-kernel" / name).read_text().splitlines():
        line = line.strip()
        if line.startswith("CONFIG_"):
            key, _, value = line.partition("=")
            out[key] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            out[line[2:].split()[0]] = "n"
    return out


class DeviceKernelTests(unittest.TestCase):
    def test_the_fragment_turns_modules_on_and_signing_off(self):
        dev = _fragment("nodo-guest-dev.config")
        self.assertEqual(dev["CONFIG_MODULES"], "y")
        self.assertEqual(dev["CONFIG_DRM"], "m")
        self.assertEqual(dev["CONFIG_FW_LOADER"], "y")
        self.assertEqual(dev["CONFIG_MODULE_SIG"], "n")
        # The default kernel keeps modules off.
        self.assertEqual(_fragment("nodo-guest.config")["CONFIG_MODULES"], "n")

    def test_build_script_asserts_the_variant(self):
        text = (ROOT / "bash" / "guest-kernel" / "build.sh").read_text()
        self.assertIn('assert_config CONFIG_MODULES y', text)
        self.assertIn('vmlinuz-dev-linux-amd64', text)
        self.assertIn('kernel-dev-build-linux-amd64.tar.xz', text)
        self.assertIn('install-extmod-build', text)

    def test_ci_publishes_the_three_assets(self):
        text = (ROOT / ".github" / "workflows" / "guest-kernel.yml").read_text()
        self.assertIn("build.sh x86_64 \"$GITHUB_WORKSPACE/out\" dev", text)
        for asset in ("vmlinuz-dev-linux-amd64", "kernel-dev-modules-linux-amd64.tar.xz",
                      "kernel-dev-build-linux-amd64.tar.xz"):
            self.assertIn(f"artifacts/{asset}", text)
        self.assertIn('test "$(wc -l < SHA256SUMS)" -eq 9', text)

    def test_installer_puts_it_in_the_store_and_never_in_kernel_paths(self):
        text = (ROOT / "bash" / "setup_linux_x86.sh").read_text()
        start = text.index("provision_device_kernel() {")
        body = text[start:text.index("\n}\n", start)]
        self.assertIn('target="${kernel_store}/${digest}/vmlinuz"', body)
        self.assertNotIn("KERNEL_PATHS", body)
        self.assertIn("provision_device_kernel\n", text[text.index("provision_cloud_hypervisor_assets() {"):])


if __name__ == "__main__":
    unittest.main()
