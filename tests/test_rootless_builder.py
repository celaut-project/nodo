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
