"""Installing nodo without sudo (PR #533, on top of phase 2 of #466).

``./install.sh`` without sudo does what needs no root itself and collects the rest
as exact commands (``bash/lib_privileged.sh``): printed, saved to a script, and run
with sudo only when the operator says so. The daemon then runs as that operator,
with CAP_NET_ADMIN only.
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
LIB_PRIVILEGED = _ROOT / "bash" / "lib_privileged.sh"
LIB_PKG = _ROOT / "bash" / "lib_pkg.sh"
INSTALL = _ROOT / "install.sh"
SETUP_SERVICE_USER = _ROOT / "bash" / "setup_service_user.sh"


def _bash(script: str, priv_file: str = "") -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NODO_PRIV_FILE": priv_file}
    return subprocess.run(
        ["bash", "-c", f'. "{LIB_PKG}"; . "{LIB_PRIVILEGED}"; {script}'],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
    )


class PrivilegedStepListTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.close()
        self.priv_file = handle.name
        self.addCleanup(os.unlink, self.priv_file)

    def test_commands_are_printed_and_saved_but_not_run_without_a_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            script = os.path.join(directory, "install-privileged.sh")
            marker = os.path.join(directory, "ran")
            result = _bash(
                f'priv_record "Step one" "touch {marker}"; '
                f'priv_offer "Needs root:" true "{script}"; echo "rc=$?"',
                self.priv_file,
            )

            self.assertIn("1. Step one", result.stdout)
            self.assertIn(f"touch {marker}", result.stdout)
            self.assertIn("rc=1", result.stdout)
            self.assertFalse(os.path.exists(marker), "a command ran without being asked")
            saved = Path(script).read_text()
            self.assertIn("set -euo pipefail", saved)
            self.assertIn(f"# Step one\ntouch {marker}", saved)
            self.assertEqual(subprocess.run(["bash", "-n", script]).returncode, 0)

    def test_no_pending_steps_asks_nothing(self):
        result = _bash('priv_offer "Needs root:" true ""; echo "rc=$?"', self.priv_file)
        self.assertEqual(result.stdout.strip(), "rc=0")

    def test_a_tab_or_newline_cannot_split_a_record(self):
        result = _bash('priv_record "a" "$(printf "x\\ty")"', self.priv_file)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(Path(self.priv_file).read_text(), "")

    def test_subid_ranges_do_not_overlap_existing_ones(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as subuid:
            subuid.write("a:100000:65536\nb:165536:65536\n")
        self.addCleanup(os.unlink, subuid.name)
        result = _bash(f'next_free_subid_start "{subuid.name}"')
        self.assertEqual(result.stdout.strip(), "231072")


class VirtiofsdAppArmorTests(unittest.TestCase):
    """The userns grant goes to a root-owned copy at a fixed path, never to config."""

    def test_the_profile_names_the_fixed_root_owned_copy(self):
        result = _bash("render_virtiofsd_apparmor_profile")
        self.assertIn(
            "profile nodo-virtiofsd /usr/local/libexec/nodo/virtiofsd flags=(unconfined) {",
            result.stdout,
        )
        self.assertIn("userns,", result.stdout)

    def test_no_installer_builds_the_profile_from_config(self):
        for path in (SETUP_SERVICE_USER, INSTALL):
            with self.subTest(path=path.name):
                content = path.read_text()
                self.assertNotIn("config_value '.virtualizers.ch.VIRTIOFSD_BINARY'", content)
                self.assertNotIn("read_config_path_or_default '.virtualizers.ch.VIRTIOFSD_BINARY'", content)
                self.assertIn("render_virtiofsd_apparmor_profile", content)


class InstallWithoutSudoTests(unittest.TestCase):
    def setUp(self):
        self.install = INSTALL.read_text()

    def test_there_is_no_root_gate_and_the_default_dir_is_the_users(self):
        self.assertNotIn("This script needs to be run with sudo", self.install)
        self.assertIn('TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nodo"', self.install)
        self.assertIn("--no-input)", self.install)

    def test_the_daemon_runs_as_the_installing_user(self):
        self.assertIn('if [ "$UNPRIVILEGED" = true ]; then\n  # Without sudo there is no other user to run it as.\n  SERVICE_USER="$SCRIPT_USER"', self.install)
        self.assertIn("nodo-nosudo.service.template", self.install)
        self.assertIn('-e "s|{{SERVICE_USER}}|$SCRIPT_USER|g"', self.install)

    def test_every_root_step_is_a_sudo_command(self):
        joined = re.sub(r"\\\n\s*", " ", self.install)
        calls = [line for line in joined.splitlines() if line.strip().startswith('priv_record "')]
        self.assertGreaterEqual(len(calls), 7)
        for line in calls:
            with self.subTest(call=line.strip()[:80]):
                command = re.match(r'\s*priv_record "(?:[^"\\]|\\.)*" (.*)', line).group(1).lstrip()
                self.assertTrue(
                    command.startswith('"sudo ') or command.startswith('"$(pkg_install_command'),
                    command,
                )

    def test_the_wrapper_and_completion_stay_in_the_users_home(self):
        self.assertIn('WRAPPER_SCRIPT="$HOME/.local/bin/nodo"', self.install)
        self.assertIn('[ "$UNPRIVILEGED" != true ] || scope=""', self.install)

    def test_service_user_needs_sudo(self):
        self.assertIn("--service-user creates a system user and needs sudo", self.install)

    def test_nodo_update_keeps_the_install_dir_and_the_mode(self):
        nodo = (_ROOT / "nodo.py").read_text()
        self.assertIn('install.sh --target-dir {main_dir}', nodo)
        self.assertIn("service_user != getpass.getuser()", nodo)


class PackageStepsTests(unittest.TestCase):
    def test_dnf_installs_the_host_dependencies(self):
        """It used to refresh the cache and never call `dnf install`."""
        lib = LIB_PKG.read_text()
        body = lib.split("pkg_install_host_dependencies() {")[1].split("\n}\n")[0]
        self.assertIn('dnf install -y "${dnf_opts[@]}" "${packages[@]}"', body)

    def test_every_host_alias_can_be_checked_without_installing(self):
        lib = LIB_PKG.read_text()
        aliases = lib.split("NODO_HOST_PACKAGE_ALIASES=(")[1].split(")")[0].split()
        for alias in aliases:
            with self.subTest(alias=alias):
                result = _bash(f"host_alias_present {alias}; echo rc=$?")
                self.assertNotIn("Unknown package alias", result.stderr)
                self.assertRegex(result.stdout, r"rc=[01]")

    def test_install_commands_per_package_manager(self):
        self.assertEqual(
            _bash('PKG_MGR=apt; pkg_install_command uidmap zip').stdout.strip(),
            "sudo apt-get update && sudo apt-get install -y --no-install-recommends uidmap zip",
        )
        self.assertEqual(
            _bash('PKG_MGR=dnf; pkg_install_command zip').stdout.strip(),
            "sudo dnf install -y zip",
        )

    def test_setup_scripts_skip_package_steps_without_root(self):
        for name in ("setup_linux_x86.sh", "setup_linux_arm.sh"):
            with self.subTest(script=name):
                content = (_ROOT / "bash" / name).read_text()
                gate = content.index('if [ "${NODO_UNPRIVILEGED:-}" != 1 ]; then\n    echo "Updating package lists..."')
                self.assertLess(gate, content.index("    pkg_install_host_dependencies"))
                self.assertIn("lib_privileged.sh", content)


if __name__ == "__main__":
    unittest.main()
