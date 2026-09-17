"""What the installer may do about Rust, and what it must never do again.

`bash/setup_linux_arm.sh` used to run rustup with its stock defaults and then
`source "$HOME/.cargo/env"` -- an export that dies with the script's own
subshell, leaving a toolchain the node could not find and reinstalled on every
`nodo tui` (issue #375). `setup_linux_x86.sh` had the mirror-image problem: it
installed no Rust at all, so the first `nodo tui` on an x86 node compiled the
crate at a moment nobody was watching.

Both now go through `bash/lib_rust.sh`, which fetches the published binary first
and only installs a toolchain when this host has to build one -- into
`$TARGET_DIR/runtime/rust`, with `--no-modify-path`.

Checked by reading the scripts because there is no other place both are visible
at once and no way to run them on a developer's machine: they need root, a
distro package manager and network. The Python half of the same contract is in
tests/test_rust_toolchain.py.
"""
import re
import unittest
from pathlib import Path

SETUP_SCRIPTS = ("bash/setup_linux_x86.sh", "bash/setup_linux_arm.sh")
LIB_RUST = "bash/lib_rust.sh"


def _code(path):
    """The script's code lines. Comments describe the old bug, so they are not it."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        yield line


class SetupScriptsRustTests(unittest.TestCase):
    def test_neither_script_installs_rust_into_home(self):
        for script in SETUP_SCRIPTS + (LIB_RUST,):
            for line in _code(script):
                with self.subTest(script=script, line=line):
                    self.assertNotIn("$HOME/.cargo", line)
                    self.assertNotIn("${HOME}/.cargo", line)
                    self.assertNotIn(".cargo/env", line)

    def test_neither_script_asks_PATH_whether_cargo_exists(self):
        # `command -v cargo` is the question that cannot be answered correctly:
        # it reports the operator's toolchain, which is not the node's.
        for script in SETUP_SCRIPTS + (LIB_RUST,):
            for line in _code(script):
                with self.subTest(script=script, line=line):
                    self.assertNotIn("command -v cargo", line)

    def test_both_scripts_define_a_rust_runtime_root_under_the_target(self):
        for script in SETUP_SCRIPTS:
            content = Path(script).read_text(encoding="utf-8")
            with self.subTest(script=script):
                self.assertIn('RUST_RUNTIME_ROOT_DEFAULT="$RUNTIME_DIR/rust"', content)
                self.assertIn(
                    ".dependencies.rust.RUNTIME_ROOT", content,
                    "the root must be overridable the same way python's is",
                )

    def test_both_scripts_read_the_override_where_they_read_pythons(self):
        # In apply_configured_dependency_paths, so a relocated root is in effect
        # before anything is installed into it.
        for script in SETUP_SCRIPTS:
            content = Path(script).read_text(encoding="utf-8")
            block = content.split("apply_configured_dependency_paths() {", 1)[1].split("\n}", 1)[0]
            with self.subTest(script=script):
                self.assertIn(".dependencies.rust.RUNTIME_ROOT", block)
                self.assertIn(".dependencies.python.RUNTIME_ROOT", block)

    def test_both_scripts_provision_rust_through_the_shared_library(self):
        for script in SETUP_SCRIPTS:
            content = Path(script).read_text(encoding="utf-8")
            with self.subTest(script=script):
                self.assertIn("lib_rust.sh", content)
                self.assertIn("provision_rust_and_tui", content)

    def test_x86_no_longer_lacks_a_rust_step(self):
        # It never had one, which is the same bug from the other side.
        content = Path("bash/setup_linux_x86.sh").read_text(encoding="utf-8")
        self.assertIn("provision_rust_and_tui", content)


class LibRustTests(unittest.TestCase):
    def setUp(self):
        self.content = Path(LIB_RUST).read_text(encoding="utf-8")

    def test_rustup_runs_with_the_node_owned_homes(self):
        self.assertIn('RUSTUP_HOME="$RUST_RUNTIME_ROOT/rustup"', self.content)
        self.assertIn('CARGO_HOME="$RUST_RUNTIME_ROOT/cargo"', self.content)

    def test_rustup_never_edits_a_shell_profile(self):
        for line in _code(LIB_RUST):
            if "sh.rustup.rs" in line:
                self.assertIn("--no-modify-path", line)
                break
        else:
            self.fail("lib_rust.sh no longer installs rustup at all")

    def test_the_install_is_idempotent(self):
        # Re-running setup on a node that already has a toolchain must not
        # reinstall it: that is the symptom the issue is named after.
        self.assertIn("rust_toolchain_ready", self.content)
        self.assertRegex(
            self.content,
            r"install_self_contained_rust\(\)[\s\S]*?if rust_toolchain_ready; then",
        )

    def test_a_prebuilt_binary_lets_the_toolchain_be_skipped(self):
        self.assertIn("fetch_prebuilt_tui", self.content)
        self.assertRegex(self.content, r"Skipping the Rust toolchain")

    def test_the_toolchain_can_be_forced_in_for_developers(self):
        self.assertIn(".dependencies.rust.INSTALL_TOOLCHAIN", self.content)
        self.assertIn("NODO_INSTALL_RUST", self.content)

    def test_the_downloaded_binary_is_checksummed_and_host_matched(self):
        self.assertIn("sha256sum", self.content)
        self.assertIn("RUST_TUI_HOST_TRIPLE", self.content)
        self.assertIn("host-triple", self.content)

    def test_a_failed_fetch_falls_back_rather_than_aborting_the_install(self):
        # Every other nodo command works without a TUI; an install that dies
        # here leaves a node that would otherwise serve.
        tail = self.content.split("provision_rust_and_tui() {", 1)[1]
        self.assertIn("installing Rust to build it from source", tail)
        self.assertIn("Warning: could not install Rust", tail)
        self.assertNotIn("fail \"Failed to install Rust", tail.split("if fetch_prebuilt_tui", 1)[1])

    def test_the_binary_is_installed_where_cargo_would_have_put_it(self):
        # One location for a shipped and a locally built binary, so neither can
        # shadow the other.
        self.assertIn("src/commands/tui/target/release", self.content)


class ConfigExampleTests(unittest.TestCase):
    def test_the_rust_dependency_is_documented_beside_its_siblings(self):
        content = Path("config.example.yaml").read_text(encoding="utf-8")
        self.assertIn("  rust:", content)
        self.assertRegex(content, r"rust:\s*\n\s*RUNTIME_ROOT:")
        self.assertIn("INSTALL_TOOLCHAIN:", content)

    def test_the_toolchain_is_not_installed_by_default(self):
        content = Path("config.example.yaml").read_text(encoding="utf-8")
        block = content.split("  rust:", 1)[1].split("  yq:", 1)[0]
        self.assertRegex(block, r"INSTALL_TOOLCHAIN:\s*false")


class TuiReleaseWorkflowTests(unittest.TestCase):
    WORKFLOW = ".github/workflows/tui-release.yml"

    def setUp(self):
        self.content = Path(self.WORKFLOW).read_text(encoding="utf-8")

    def test_it_parses(self):
        yaml = __import__("yaml")
        self.assertIsInstance(yaml.safe_load(self.content), dict)

    def test_both_supported_linux_targets_are_built(self):
        self.assertIn("x86_64-unknown-linux-gnu", self.content)
        self.assertIn("aarch64-unknown-linux-gnu", self.content)

    def test_native_runners_rather_than_cross(self):
        # The crate bundles SQLite's C sources and runs a build.rs; a native
        # runner for each arch is available, so nothing is cross-compiled.
        self.assertIn("ubuntu-24.04-arm", self.content)
        self.assertNotIn("cross build", self.content)

    def test_each_binary_ships_its_marker_and_a_checksum(self):
        self.assertIn("host-triple", self.content)
        self.assertIn("sha256sum", self.content)
        self.assertIn("rustc -vV", self.content)

    def test_the_marker_comes_from_the_compiler_not_from_the_matrix(self):
        # The two disagreeing is the mislabelled-release case the marker exists
        # to catch, so the matrix cannot be the source of it.
        self.assertRegex(self.content, r"HOST=.*rustc -vV")
        self.assertIn("matrix expected", self.content)

    def test_a_pull_request_builds_but_publishes_nothing(self):
        self.assertIn("pull_request:", self.content)
        self.assertRegex(self.content, r"if:\s*github\.event_name != 'pull_request'")

    def test_the_build_job_cannot_write_to_the_repository(self):
        # Same least-privilege split guest-kernel.yml uses.
        self.assertRegex(self.content, r"permissions:\s*\n\s*contents: read")
        self.assertRegex(self.content, r"permissions:\s*\n\s*contents: write")

    def test_the_asset_names_are_the_ones_the_installer_asks_for(self):
        lib = Path(LIB_RUST).read_text(encoding="utf-8")
        self.assertIn('asset="tui-${RUST_TUI_ASSET_TAG}"', lib)
        for script, tag in (
            ("bash/setup_linux_x86.sh", "linux-amd64"),
            ("bash/setup_linux_arm.sh", "linux-arm64"),
        ):
            with self.subTest(script=script):
                self.assertIn(
                    f'RUST_TUI_ASSET_TAG="{tag}"',
                    Path(script).read_text(encoding="utf-8"),
                )
                self.assertIn(f"tui-{tag}", self.content)

    def test_the_release_tag_matches_what_the_installer_downloads(self):
        lib = Path(LIB_RUST).read_text(encoding="utf-8")
        tag = re.search(r'RUST_TUI_RELEASE_TAG="\$\{RUST_TUI_RELEASE_TAG:-([^}"]+)\}"', lib)
        self.assertIsNotNone(tag, "lib_rust.sh no longer names a release tag")
        self.assertIn(f'- "{tag.group(1)}"', self.content)


class TuiVersionFlagTests(unittest.TestCase):
    def test_the_rust_binary_answers_version(self):
        # nodo validates a downloaded binary by running it, and every other
        # entry point takes over the terminal.
        main_rs = Path("src/commands/tui/src/main.rs").read_text(encoding="utf-8")
        self.assertIn('"--version"', main_rs)
        self.assertIn("CARGO_PKG_VERSION", main_rs)


if __name__ == "__main__":
    unittest.main()
