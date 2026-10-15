"""The setup scripts provision the Rust virtiofsd (#478), in bash/lib_virtiofsd.sh.

The decision is tested by running provision_virtiofsd in bash with the download,
the build and yq replaced by stubs. The real download and build were checked by
hand in a container (see the PR).
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

LIB = Path("bash/lib_virtiofsd.sh")
SETUP_SCRIPTS = (Path("bash/setup_linux_arm.sh"), Path("bash/setup_linux_x86.sh"))


class SetupScriptWiringTests(unittest.TestCase):
    def test_both_setup_scripts_provision_virtiofsd_after_rust(self):
        for script in SETUP_SCRIPTS:
            with self.subTest(script=str(script)):
                content = script.read_text(encoding="utf-8")
                self.assertIn('. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib_virtiofsd.sh"', content)
                # After the Rust step, so a build from source reuses its toolchain,
                # and after the portable Python, which extracts the zip.
                call = content.index("\nprovision_virtiofsd\n")
                self.assertLess(content.index("\nprovision_rust_and_tui\n"), call)
                self.assertLess(content.index("\ninstall_portable_python\n"), call)

    def test_prebuilt_binary_is_pinned_by_full_digests(self):
        content = LIB.read_text(encoding="utf-8")
        for name in ("VIRTIOFSD_AMD64_ZIP_SHA256", "VIRTIOFSD_AMD64_BIN_SHA256"):
            with self.subTest(name=name):
                self.assertRegex(content, rf'{name}="[0-9a-f]{{64}}"')
        version = re.search(r'VIRTIOFSD_VERSION="([^"]+)"', content).group(1)
        url = re.search(r'VIRTIOFSD_AMD64_ZIP_URL="([^"]+)"', content).group(1)
        self.assertTrue(url.startswith("https://gitlab.com/"))
        self.assertIn(f"virtiofsd-v{version}.zip", url)

    def test_build_deps_go_through_lib_pkg(self):
        self.assertNotIn("apt-get", LIB.read_text(encoding="utf-8"))
        self.assertNotIn("dnf ", LIB.read_text(encoding="utf-8"))
        self.assertIn("install_virtiofsd_build_deps()", Path("bash/lib_pkg.sh").read_text(encoding="utf-8"))


@unittest.skipIf(shutil.which("bash") is None, "bash is not available")
class ProvisionDecisionTests(unittest.TestCase):
    def _run(self, configured, *, prebuilt_ok=True, build_ok=True, already=False):
        with tempfile.TemporaryDirectory() as tmp:
            yq_log = Path(tmp) / "yq.log"
            script = f"""
set -euo pipefail
TARGET_DIR="{tmp}/nodo"
CONFIG_FILE="{tmp}/config.yaml"
YQ_BIN="{tmp}/yq"
printf '#!/bin/sh\\necho "$VIRTIOFSD_TARGET $*" >> "{yq_log}"\\n' > "$YQ_BIN"
chmod +x "$YQ_BIN"
. "{LIB.resolve()}"
read_config_path_or_default() {{ printf '%s' "{configured}"; }}
INSTALLED=no
virtiofsd_is_pinned_version() {{ [ "{'yes' if already else ''}" = yes ] || [ "$INSTALLED" = yes ]; }}
fetch_prebuilt_virtiofsd() {{ echo FETCH; [ "{int(prebuilt_ok)}" = 1 ] && INSTALLED=yes; }}
build_virtiofsd_from_source() {{ echo BUILD; [ "{int(build_ok)}" = 1 ] && INSTALLED=yes; }}
provision_virtiofsd
echo "TARGET=$(virtiofsd_target)"
"""
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            yq = yq_log.read_text() if yq_log.exists() else ""
            return result, yq, f"{tmp}/nodo/bin/virtiofsd"

    def test_custom_path_is_kept_and_nothing_is_installed(self):
        result, yq, _target = self._run("/opt/virtiofsd/virtiofsd")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("keeping it", result.stdout)
        self.assertNotIn("FETCH", result.stdout)
        self.assertNotIn("BUILD", result.stdout)
        self.assertEqual(yq, "")

    def test_default_value_gets_the_prebuilt_binary_and_its_absolute_path(self):
        result, yq, target = self._run("virtiofsd")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FETCH", result.stdout)
        self.assertNotIn("BUILD", result.stdout)
        self.assertIn(target, yq)
        self.assertIn(".virtualizers.ch.VIRTIOFSD_BINARY = strenv(VIRTIOFSD_TARGET)", yq)

    def test_build_from_source_when_no_prebuilt_fits(self):
        result, yq, target = self._run("virtiofsd", prebuilt_ok=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BUILD", result.stdout)
        self.assertIn(target, yq)

    def test_failure_warns_and_does_not_fail_the_install_or_touch_config(self):
        result, yq, _target = self._run("virtiofsd", prebuilt_ok=False, build_ok=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not install virtiofsd", result.stderr)
        self.assertIn("TARGET=", result.stdout)
        self.assertEqual(yq, "")

    def test_already_installed_is_not_downloaded_again(self):
        result, yq, target = self._run("virtiofsd", already=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already installed", result.stdout)
        self.assertNotIn("FETCH", result.stdout)
        self.assertIn(target, yq)


if __name__ == "__main__":
    unittest.main()
