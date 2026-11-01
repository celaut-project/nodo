"""uninstall.sh removes the AppArmor profile of nodo's rootlesskit (#608).

bash/install_buildkit.sh writes /etc/apparmor.d/nodo-rootlesskit, a userns
exemption for a path under the install directory. The script is only ever run
here with stubs on PATH, a temporary install directory, and the profile path
overridden to a temporary file: never against the real system.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "uninstall.sh"

PROFILE = """abi <abi/4.0>,
include <tunables/global>

profile nodo-rootlesskit {bin}/rootlesskit flags=(unconfined) {{
  userns,
}}
"""


class UninstallRootlesskitProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = os.path.join(self.tmp, "opt", "nodo")
        os.makedirs(self.target)
        Path(self.target, "nodo.py").write_text("")
        self.profile = os.path.join(self.tmp, "nodo-rootlesskit")
        self.log = os.path.join(self.tmp, "stub.log")
        self.stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(self.stubs)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

    def _stub(self, name, body):
        path = os.path.join(self.stubs, name)
        Path(path).write_text("#!/bin/bash\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def _run(self, *args, as_root=False):
        for name in ("pgrep", "pkill", "findmnt", "systemctl", "umount"):
            self._stub(name, "exit 1" if name == "pgrep" else "exit 0")
        self._stub("apparmor_parser", f'echo "apparmor_parser $*" >> "{self.log}"')
        if as_root:
            self._stub("id", "echo 0")
        env = {
            "PATH": self.stubs + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home,
            "NODO_ROOTLESSKIT_APPARMOR_PROFILE": self.profile,
            # Never the real files, whatever the machine has.
            "NODO_VIRTIOFSD_APPARMOR_PROFILE": os.path.join(self.tmp, "none-virtiofsd"),
            "NODO_SERVICE_USER_RECORD": os.path.join(self.tmp, "none-service-user"),
            "NODO_VIRTIOFSD_SYSTEM_PATH": os.path.join(self.tmp, "none-virtiofsd-bin"),
        }
        return subprocess.run(
            ["setsid", "bash", str(SCRIPT), "--target-dir", self.target, *args],
            capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, cwd=self.tmp,
        )

    def _write_profile(self, bin_dir):
        Path(self.profile).write_text(PROFILE.format(bin=bin_dir))

    def _parser_calls(self):
        return Path(self.log).read_text() if os.path.exists(self.log) else ""

    # The plan: no --yes and no terminal, so nothing is removed.
    def test_plan_removes_the_profile_that_names_the_target(self):
        self._write_profile(f"{self.target}/bin")
        out = self._run("--no-input").stdout
        removed = out.split("Kept:")[0]
        self.assertIn(self.profile, removed)
        self.assertTrue(os.path.exists(self.profile))

    def test_plan_keeps_the_profile_of_another_install(self):
        self._write_profile(os.path.join(self.tmp, "opt", "nodo2", "bin"))
        out = self._run("--no-input").stdout
        self.assertNotIn(self.profile, out.split("Kept:")[0])
        self.assertIn(f"{self.profile}: it does not name {self.target}", out)

    def test_plan_without_a_profile_says_nothing_of_it(self):
        out = self._run("--no-input").stdout
        self.assertNotIn(self.profile, out)

    # Without sudo: the root step is printed and saved, not run.
    def test_without_sudo_the_root_step_is_recorded(self):
        self._write_profile(f"{self.target}/bin")
        result = self._run("--no-input", "--yes")
        saved = Path(self.target, "uninstall-privileged.sh").read_text()
        self.assertIn(f"sudo apparmor_parser -R {self.profile}", saved)
        self.assertIn(f"sudo rm -f {self.profile}", saved)
        self.assertTrue(os.path.exists(self.profile), result.stdout)

    def test_without_sudo_another_install_gets_no_root_step(self):
        self._write_profile(os.path.join(self.tmp, "opt", "nodo2", "bin"))
        self._run("--no-input", "--yes")
        saved = Path(self.target, "uninstall-privileged.sh")
        self.assertFalse(saved.exists() and "nodo-rootlesskit" in saved.read_text())

    # With sudo (id reports root, tools are stubs): the profile is unloaded and removed.
    def test_with_sudo_the_profile_is_unloaded_and_removed(self):
        self._write_profile(f"{self.target}/bin")
        result = self._run("--yes", as_root=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"apparmor_parser -R {self.profile}", self._parser_calls())
        self.assertFalse(os.path.exists(self.profile))
        self.assertFalse(os.path.exists(self.target))

    def test_with_sudo_another_install_keeps_its_profile(self):
        self._write_profile(os.path.join(self.tmp, "opt", "nodo2", "bin"))
        result = self._run("--yes", as_root=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("nodo-rootlesskit", self._parser_calls())
        self.assertTrue(os.path.exists(self.profile))

    def test_with_sudo_without_a_profile_nothing_is_unloaded(self):
        result = self._run("--yes", as_root=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._parser_calls(), "")


if __name__ == "__main__":
    unittest.main()
