"""The local packer's builder must stay rootless.

These are regression tests for the failure that motivated the switch away from
the isolated dockerd: that daemon was started through sudo, so an unprivileged
`nodo pack` could not signal it. A failed build left it alive holding its
data-root lock, the stop script deleted the live daemon's socket while reporting
success, and every later pack died with "already running but is not responding".

The fix is structural — the builder runs as the invoking user — so what is worth
pinning is the structure: no privileged call anywhere in the pack path.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BASH_DIR = Path("bash")


class RootlessBuilderScriptsTests(unittest.TestCase):
    def test_pack_path_scripts_exist(self):
        for name in ("lib_rootless.sh", "start_buildkit_daemon.sh", "stop_buildkit_daemon.sh"):
            self.assertTrue((BASH_DIR / name).is_file(), f"missing {name}")

    def test_pack_path_never_invokes_sudo(self):
        # The builder belongs to the invoking user, so starting and stopping it
        # must never escalate. Only a *call* counts: these scripts are allowed to
        # mention sudo in comments and in the message pointing at the installer,
        # so match sudo in command position (and any ${SUDO} expansion) instead.
        invocation = re.compile(r"(^|[;&|(]|\bthen\b|\bdo\b|\bxargs\b)\s*sudo\b|\$\{?SUDO\}?")
        for name in ("start_buildkit_daemon.sh", "stop_buildkit_daemon.sh", "lib_rootless.sh"):
            for lineno, line in enumerate((BASH_DIR / name).read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                self.assertIsNone(
                    invocation.search(code),
                    f"{name}:{lineno} runs sudo in the pack path: {line.strip()}",
                )

    def test_installer_is_the_only_privileged_script(self):
        # Provisioning host prerequisites (uidmap, rootlesskit, subuid ranges) is
        # genuinely host-wide, so the installer may escalate — once.
        installer = (BASH_DIR / "install_buildkit.sh").read_text(encoding="utf-8")
        self.assertIn("SUDO=", installer)

    def test_retired_docker_toolchain_is_gone(self):
        for name in (
            "install_docker.sh",
            "lib_docker_daemon.sh",
            "start_docker_daemon.sh",
            "stop_docker_daemon.sh",
            "setup_docker_daemon.sh",
        ):
            self.assertFalse((BASH_DIR / name).exists(), f"{name} should have been removed")
        for name in ("src/utils/docker_env.py", "src/utils/docker_dependency.py"):
            self.assertFalse(Path(name).exists(), f"{name} should have been removed")

    def test_stop_script_keeps_a_live_builders_socket(self):
        # The old stop script deleted the socket of a daemon it had failed to
        # kill, which wedged it permanently: the daemon never recreates it.
        stop = (BASH_DIR / "stop_buildkit_daemon.sh").read_text(encoding="utf-8")
        guard = stop.index("REMAINING=")
        removal = stop.index('rm -f "${BUILDKIT_PID_FILE}" "${BUILDKIT_SOCKET}"')
        self.assertLess(guard, removal, "the socket must only be removed after the liveness guard")


class RootlessPrereqResolutionTests(unittest.TestCase):
    """rootlesskit is not required to be on PATH.

    On distros with no rootlesskit package (Arch, Alpine) the installer drops the
    upstream static binary in MAIN_DIR/bin, which is only on PATH when nodo is
    invoked through its wrapper. A `command -v` probe therefore reported the
    dependency missing right after installing it, so install_buildkit.sh failed
    its own post-provision recheck and start_buildkit_daemon.sh refused to launch.
    """

    def _missing(self, bin_dir):
        script = (
            f'. "{BASH_DIR.resolve()}/lib_rootless.sh"; '
            f'rootless_prereqs_missing "{bin_dir}"'
        )
        out = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )
        return out.stdout.split()

    @unittest.skipIf(shutil.which("bash") is None, "bash required")
    def test_rootlesskit_in_bin_dir_counts_as_present(self):
        if Path("/usr/bin/rootlesskit").exists() or shutil.which("rootlesskit"):
            self.skipTest("host provides rootlesskit; the fallback path is not exercised")
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "rootlesskit"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            self.assertNotIn(
                "rootlesskit",
                self._missing(tmp),
                "a rootlesskit installed under MAIN_DIR/bin must not be reported missing",
            )

    @unittest.skipIf(shutil.which("bash") is None, "bash required")
    def test_absent_rootlesskit_is_still_reported(self):
        if Path("/usr/bin/rootlesskit").exists() or shutil.which("rootlesskit"):
            self.skipTest("host provides rootlesskit; absence cannot be simulated")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("rootlesskit", self._missing(tmp))

    def test_prereq_check_does_not_rely_on_path_lookup_for_rootlesskit(self):
        lib = (BASH_DIR / "lib_rootless.sh").read_text(encoding="utf-8")
        body = lib[lib.index("rootless_prereqs_missing()"):]
        body = body[: body.index("\n}")]
        self.assertNotIn(
            "command -v rootlesskit",
            body,
            "rootlesskit must be resolved via resolve_rootlesskit, not a bare PATH probe",
        )

    def test_callers_pass_the_bin_dir(self):
        for name in ("install_buildkit.sh", "start_buildkit_daemon.sh"):
            content = (BASH_DIR / name).read_text(encoding="utf-8")
            for call in re.findall(r"rootless_prereqs_missing[^\n]*", content):
                if call.strip().startswith("#"):
                    continue
                self.assertRegex(
                    call,
                    r"rootless_prereqs_missing\s+\"?\$",
                    f"{name}: rootless_prereqs_missing must be given the bin dir: {call}",
                )


class SubordinateIdRangeTests(unittest.TestCase):
    """usermod does not deduplicate subordinate ranges across users, so a fixed
    100000 base silently collides with an existing rootless Docker/Podman
    allocation and two users end up sharing a namespace id range."""

    def _pick(self, contents):
        installer = (BASH_DIR / "install_buildkit.sh").read_text(encoding="utf-8")
        start = installer.index("next_free_subid_start()")
        fn = installer[start : installer.index("\n}", start) + 2]
        with tempfile.NamedTemporaryFile("w", suffix=".subid", delete=False) as fh:
            fh.write(contents)
            path = fh.name
        try:
            out = subprocess.run(
                ["bash", "-c", f'{fn}\nnext_free_subid_start "{path}"'],
                capture_output=True,
                text=True,
                check=False,
            )
            return out.stdout.strip()
        finally:
            Path(path).unlink(missing_ok=True)

    @unittest.skipIf(shutil.which("bash") is None, "bash required")
    def test_unallocated_host_uses_the_conventional_base(self):
        self.assertEqual(self._pick(""), "100000")

    @unittest.skipIf(shutil.which("bash") is None, "bash required")
    def test_steps_past_an_existing_allocation(self):
        self.assertEqual(self._pick("dockeruser:100000:65536\n"), "165536")

    @unittest.skipIf(shutil.which("bash") is None, "bash required")
    def test_steps_past_several_including_unsorted_input(self):
        self.assertEqual(self._pick("b:165536:65536\na:100000:65536\n"), "231072")

    @unittest.skipIf(shutil.which("bash") is None, "bash required")
    def test_reuses_a_free_low_range(self):
        self.assertEqual(self._pick("b:900000:65536\n"), "100000")

    @unittest.skipIf(shutil.which("bash") is None, "bash required")
    def test_ignores_malformed_lines(self):
        self.assertEqual(self._pick("#comment\nbad:line\na:100000:65536\n"), "165536")


class DownloadIntegrityTests(unittest.TestCase):
    def test_pinned_releases_have_pinned_digests(self):
        # verify_sha256 is a no-op when the expected digest is empty, so relying on
        # NODO_*_SHA256 being set means the default install verifies nothing.
        installer = (BASH_DIR / "install_buildkit.sh").read_text(encoding="utf-8")
        for name in (
            "BUILDKIT_SHA256_linux_amd64",
            "BUILDKIT_SHA256_linux_arm64",
            "ROOTLESSKIT_SHA256_x86_64",
            "ROOTLESSKIT_SHA256_aarch64",
        ):
            match = re.search(rf'{name}="([0-9a-f]*)"', installer)
            self.assertIsNotNone(match, f"{name} is not defined")
            self.assertEqual(len(match.group(1)), 64, f"{name} is not a sha256 digest")

    def test_downloads_verify_by_default(self):
        installer = (BASH_DIR / "install_buildkit.sh").read_text(encoding="utf-8")
        for var in ("NODO_BUILDKIT_SHA256", "NODO_ROOTLESSKIT_SHA256"):
            self.assertIn(
                f"${{{var}:-$",
                installer,
                f"{var} must fall back to the pinned digest, not to an empty string",
            )


class RootlessBuilderConfigTests(unittest.TestCase):
    def test_config_example_declares_buildkit_paths(self):
        content = Path("config.example.yaml").read_text(encoding="utf-8")
        self.assertIn("buildkit:", content)
        self.assertIn("DAEMON_BIN:", content)
        self.assertIn("BUILDKIT_SOCKET:", content)

    def test_config_example_drops_the_docker_toolchain_keys(self):
        content = Path("config.example.yaml").read_text(encoding="utf-8")
        for key in ("BUILDX_BIN:", "DOCKER_SOCKET:", "BUILDX_BUILDER:", "BUILDX_NETWORK:"):
            self.assertNotIn(key, content, f"{key} belongs to the retired Docker toolchain")


if __name__ == "__main__":
    unittest.main()
