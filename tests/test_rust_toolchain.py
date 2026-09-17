"""`nodo tui` must not depend on the shell it was launched from.

The bug this covers (issue #375) was not that the search was wrong; it was that
there was a search at all. `check_rust_installation` asked `$PATH` for `rustc`,
the setup script had installed one into `$HOME/.cargo` and exported a PATH that
died with its own subshell, and the two never met -- so every launch reinstalled
a toolchain that was already there, into whichever `$HOME` the invocation
happened to have.

So what is asserted here is mostly *absence*: that a `cargo` on `$PATH` is not
consulted, that an existing `~/.cargo` is not consulted, and that the paths come
out the same however hostile the environment is. Plus the marker, which is what
stops a prebuilt for another architecture being executed -- its failure mode is
`Exec format error`, which an operator experiences as `nodo tui` doing nothing.
"""
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from src.utils import rust_toolchain


def _executable(path, body="#!/bin/sh\nexit 0\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _Completed:
    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


class PathResolutionTests(unittest.TestCase):
    """Fixed paths under the installation root, whatever the environment says."""

    def test_defaults_sit_under_the_installation_root(self):
        with mock.patch.object(rust_toolchain, "_config_get", return_value=None):
            self.assertEqual(
                rust_toolchain.runtime_root("/nodo"), os.path.join("/nodo", "runtime", "rust")
            )
            self.assertEqual(rust_toolchain.cargo_home("/nodo"), "/nodo/runtime/rust/cargo")
            self.assertEqual(rust_toolchain.rustup_home("/nodo"), "/nodo/runtime/rust/rustup")
            self.assertEqual(rust_toolchain.cargo_bin("/nodo"), "/nodo/runtime/rust/cargo/bin/cargo")
            self.assertEqual(rust_toolchain.rustc_bin("/nodo"), "/nodo/runtime/rust/cargo/bin/rustc")

    def test_the_runtime_root_config_key_relocates_everything(self):
        def configured(key):
            return "/elsewhere/rust" if key == rust_toolchain.RUNTIME_ROOT_KEY else None

        with mock.patch.object(rust_toolchain, "_config_get", side_effect=configured):
            self.assertEqual(rust_toolchain.cargo_bin("/nodo"), "/elsewhere/rust/cargo/bin/cargo")

    def test_the_main_dir_placeholder_is_expanded(self):
        def configured(key):
            if key == rust_toolchain.RUNTIME_ROOT_KEY:
                return "${main.MAIN_DIR}/runtime/rust"
            return None

        with mock.patch.object(rust_toolchain, "_config_get", side_effect=configured):
            self.assertEqual(rust_toolchain.runtime_root("/srv/nodo"), "/srv/nodo/runtime/rust")

    def test_paths_ignore_PATH_and_HOME_entirely(self):
        # The environment named here is the one the old code would have believed.
        hostile = {
            "PATH": "/opt/rust/bin:/usr/bin",
            "HOME": "/root",
            "CARGO_HOME": "/root/.cargo",
            "RUSTUP_HOME": "/root/.rustup",
        }
        with mock.patch.dict(os.environ, hostile, clear=True), mock.patch.object(
            rust_toolchain, "_config_get", return_value=None
        ):
            self.assertEqual(rust_toolchain.cargo_bin("/nodo"), "/nodo/runtime/rust/cargo/bin/cargo")

    def test_the_subprocess_environment_points_rustup_at_the_node(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/root"}, clear=True), \
                mock.patch.object(rust_toolchain, "_config_get", return_value=None):
            env = rust_toolchain.toolchain_env("/nodo")
        self.assertEqual(env["CARGO_HOME"], "/nodo/runtime/rust/cargo")
        self.assertEqual(env["RUSTUP_HOME"], "/nodo/runtime/rust/rustup")
        # cargo shells out to `rustc` by name, so ours has to come first.
        self.assertTrue(env["PATH"].startswith("/nodo/runtime/rust/cargo/bin" + os.pathsep))


class ToolchainPresenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(rust_toolchain, "_config_get", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_absent_when_there_is_no_cargo(self):
        self.assertFalse(rust_toolchain.toolchain_present(self.root))

    def test_a_cargo_on_PATH_is_not_this_nodes_cargo(self):
        # The exact situation that produced the bug: a perfectly good toolchain
        # the operator installed, which the node must still regard as absent.
        bin_dir = os.path.join(self.root, "operator", "bin")
        _executable(os.path.join(bin_dir, "cargo"))
        with mock.patch.dict(os.environ, {"PATH": bin_dir}, clear=False):
            self.assertFalse(rust_toolchain.toolchain_present(self.root))

    def test_an_existing_home_cargo_is_ignored(self):
        home = os.path.join(self.root, "home")
        _executable(os.path.join(home, ".cargo", "bin", "cargo"))
        with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
            self.assertFalse(rust_toolchain.toolchain_present(self.root))
        # And it is still there: nothing migrates or deletes the operator's.
        self.assertTrue(os.path.exists(os.path.join(home, ".cargo", "bin", "cargo")))

    def test_present_when_the_nodes_own_cargo_runs(self):
        _executable(os.path.join(self.root, "runtime", "rust", "cargo", "bin", "cargo"))
        self.assertTrue(
            rust_toolchain.toolchain_present(self.root, runner=lambda *a, **k: _Completed(0))
        )

    def test_a_cargo_that_does_not_run_is_not_a_toolchain(self):
        # A half-finished rustup leaves the directory behind.
        _executable(os.path.join(self.root, "runtime", "rust", "cargo", "bin", "cargo"))
        self.assertFalse(
            rust_toolchain.toolchain_present(self.root, runner=lambda *a, **k: _Completed(1))
        )

    def test_no_reinstall_when_the_self_contained_cargo_exists(self):
        _executable(os.path.join(self.root, "runtime", "rust", "cargo", "bin", "cargo"))
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return _Completed(0)

        self.assertTrue(rust_toolchain.ensure_toolchain(self.root, runner=runner, printer=lambda *a, **k: None))
        self.assertTrue(all("rustup" not in str(call) for call in calls), calls)


class HostTripleTests(unittest.TestCase):
    def test_amd64_linux(self):
        with mock.patch.object(rust_toolchain, "host_arch_tag", return_value="linux/amd64"), \
                mock.patch("platform.system", return_value="Linux"), \
                mock.patch.object(rust_toolchain, "_libc_tag", return_value="gnu"):
            self.assertEqual(rust_toolchain.host_triple(), "x86_64-unknown-linux-gnu")

    def test_arm64_linux(self):
        with mock.patch.object(rust_toolchain, "host_arch_tag", return_value="linux/arm64"), \
                mock.patch("platform.system", return_value="Linux"), \
                mock.patch.object(rust_toolchain, "_libc_tag", return_value="gnu"):
            self.assertEqual(rust_toolchain.host_triple(), "aarch64-unknown-linux-gnu")

    def test_musl_is_a_different_triple(self):
        with mock.patch.object(rust_toolchain, "host_arch_tag", return_value="linux/amd64"), \
                mock.patch("platform.system", return_value="Linux"), \
                mock.patch.object(rust_toolchain, "_libc_tag", return_value="musl"):
            self.assertEqual(rust_toolchain.host_triple(), "x86_64-unknown-linux-musl")

    def test_an_architecture_nodo_has_no_name_for_yields_no_triple(self):
        # None must not be guessed into something plausible: the marker check
        # would then pass for a binary that cannot run here.
        with mock.patch.object(rust_toolchain, "host_arch_tag", return_value=None):
            self.assertIsNone(rust_toolchain.host_triple())


class PrebuiltValidityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(rust_toolchain, "_config_get", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.binary, self.marker = rust_toolchain.prebuilt_paths(self.root)

    def _ship(self, triple="aarch64-unknown-linux-gnu", executable=True):
        _executable(self.binary)
        if not executable:
            os.chmod(self.binary, 0o644)
        if triple is not None:
            rust_toolchain.write_marker(self.marker, triple)

    def test_the_binary_lands_where_cargo_would_have_put_it(self):
        # One location, so a shipped binary and a locally built one are the same
        # file and neither can shadow the other.
        self.assertEqual(
            self.binary, os.path.join(self.root, "src", "commands", "tui", "target", "release", "tui")
        )
        self.assertEqual(self.marker, self.binary + ".host-triple")

    def test_a_matching_marker_is_accepted(self):
        self._ship("aarch64-unknown-linux-gnu")
        with mock.patch.object(rust_toolchain, "host_triple", return_value="aarch64-unknown-linux-gnu"):
            self.assertEqual(
                rust_toolchain.valid_prebuilt(self.root, runner=lambda *a, **k: _Completed(0)),
                self.binary,
            )

    def test_a_mismatched_marker_is_refused(self):
        self._ship("aarch64-unknown-linux-gnu")
        with mock.patch.object(rust_toolchain, "host_triple", return_value="x86_64-unknown-linux-gnu"):
            self.assertIsNone(
                rust_toolchain.valid_prebuilt(self.root, runner=lambda *a, **k: _Completed(0))
            )

    def test_a_missing_marker_is_refused(self):
        self._ship(triple=None)
        with mock.patch.object(rust_toolchain, "host_triple", return_value="aarch64-unknown-linux-gnu"):
            self.assertIsNone(
                rust_toolchain.valid_prebuilt(self.root, runner=lambda *a, **k: _Completed(0))
            )

    def test_a_non_executable_binary_is_refused(self):
        self._ship("aarch64-unknown-linux-gnu", executable=False)
        with mock.patch.object(rust_toolchain, "host_triple", return_value="aarch64-unknown-linux-gnu"):
            self.assertIsNone(
                rust_toolchain.valid_prebuilt(self.root, runner=lambda *a, **k: _Completed(0))
            )

    def test_a_binary_that_cannot_start_is_refused(self):
        self._ship("aarch64-unknown-linux-gnu")
        with mock.patch.object(rust_toolchain, "host_triple", return_value="aarch64-unknown-linux-gnu"):
            self.assertIsNone(
                rust_toolchain.valid_prebuilt(self.root, runner=lambda *a, **k: _Completed(1))
            )

    def test_the_check_is_version_and_never_the_interface(self):
        self._ship("aarch64-unknown-linux-gnu")
        seen = []

        def runner(cmd, **kwargs):
            seen.append(cmd)
            return _Completed(0)

        with mock.patch.object(rust_toolchain, "host_triple", return_value="aarch64-unknown-linux-gnu"):
            rust_toolchain.valid_prebuilt(self.root, runner=runner)
        self.assertEqual(seen, [[self.binary, "--version"]])

    def test_a_marker_with_trailing_content_still_matches(self):
        _executable(self.binary)
        with open(self.marker, "w", encoding="utf-8") as handle:
            handle.write("aarch64-unknown-linux-gnu\nbuilt by tui-release.yml\n")
        self.assertEqual(rust_toolchain.read_marker(self.marker), "aarch64-unknown-linux-gnu")

    def test_an_unreadable_marker_reads_as_none(self):
        self.assertIsNone(rust_toolchain.read_marker(os.path.join(self.root, "nope")))


class ResolutionOrderTests(unittest.TestCase):
    """Prebuilt, then our cargo, then install ours. Never a cargo from `$PATH`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(rust_toolchain, "_config_get", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.binary, self.marker = rust_toolchain.prebuilt_paths(self.root)
        self.cargo = rust_toolchain.cargo_bin(self.root)

    def test_a_valid_prebuilt_compiles_nothing(self):
        _executable(self.binary)
        rust_toolchain.write_marker(self.marker, "aarch64-unknown-linux-gnu")
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return _Completed(0)

        with mock.patch.object(rust_toolchain, "host_triple", return_value="aarch64-unknown-linux-gnu"):
            resolved = rust_toolchain.resolve_tui(self.root, runner=runner, printer=lambda *a, **k: None)

        self.assertEqual(resolved, self.binary)
        self.assertEqual(calls, [[self.binary, "--version"]])

    def test_no_source_and_no_prebuilt_says_so(self):
        # A node installed from a release rootfs has no crate to build. Without
        # this the operator got `[Errno 2]` naming a path, which reads as a
        # broken install rather than as a missing binary for this architecture.
        _executable(self.cargo)
        messages = []
        resolved = rust_toolchain.resolve_tui(
            self.root,
            runner=lambda *a, **k: _Completed(0),
            printer=lambda m, **k: messages.append(m),
        )
        self.assertIsNone(resolved)
        self.assertTrue(any("No TUI source" in m for m in messages), messages)

    def test_no_prebuilt_but_our_cargo_builds_without_installing(self):
        _executable(self.cargo)
        os.makedirs(os.path.join(self.root, rust_toolchain.TUI_CRATE_RELPATH), exist_ok=True)
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            if isinstance(cmd, list) and cmd[1:] == ["build", "--release"]:
                _executable(self.binary)
            if isinstance(cmd, list) and cmd[1:] == ["-vV"]:
                return _Completed(0, b"rustc 1.0.0\nhost: aarch64-unknown-linux-gnu\n")
            return _Completed(0)

        resolved = rust_toolchain.resolve_tui(self.root, runner=runner, printer=lambda *a, **k: None)

        self.assertEqual(resolved, self.binary)
        self.assertIn([self.cargo, "build", "--release"], calls)
        self.assertTrue(all("rustup" not in str(call) for call in calls), calls)
        # Building writes the marker, so the *next* launch takes the fast path.
        self.assertEqual(
            rust_toolchain.read_marker(self.marker), "aarch64-unknown-linux-gnu"
        )

    def test_neither_present_installs_into_the_installation_root(self):
        os.makedirs(os.path.join(self.root, rust_toolchain.TUI_CRATE_RELPATH), exist_ok=True)
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if isinstance(cmd, str) and "sh.rustup.rs" in cmd:
                _executable(self.cargo)
            if isinstance(cmd, list) and cmd[1:] == ["build", "--release"]:
                _executable(self.binary)
            if isinstance(cmd, list) and cmd[1:] == ["-vV"]:
                return _Completed(0, b"host: x86_64-unknown-linux-gnu\n")
            return _Completed(0)

        resolved = rust_toolchain.resolve_tui(self.root, runner=runner, printer=lambda *a, **k: None)
        self.assertEqual(resolved, self.binary)

        install = next(c for c in calls if isinstance(c[0], str) and "sh.rustup.rs" in c[0])
        command, kwargs = install
        # --no-modify-path: the whole bug came from a toolchain that announced
        # itself by editing a shell profile nobody re-read.
        self.assertIn("--no-modify-path", command)
        self.assertEqual(kwargs["env"]["CARGO_HOME"], os.path.join(self.root, "runtime", "rust", "cargo"))
        self.assertEqual(kwargs["env"]["RUSTUP_HOME"], os.path.join(self.root, "runtime", "rust", "rustup"))
        # The directories it was told to use are under the installation root.
        self.assertTrue(os.path.isdir(os.path.join(self.root, "runtime", "rust", "rustup")))

    def test_a_cargo_on_PATH_is_never_the_one_invoked(self):
        os.makedirs(os.path.join(self.root, rust_toolchain.TUI_CRATE_RELPATH), exist_ok=True)
        elsewhere = os.path.join(self.root, "operator", "bin")
        _executable(os.path.join(elsewhere, "cargo"))
        invoked = []

        def runner(cmd, **kwargs):
            invoked.append(cmd)
            if isinstance(cmd, str) and "sh.rustup.rs" in cmd:
                _executable(self.cargo)
            if isinstance(cmd, list) and cmd[1:] == ["build", "--release"]:
                _executable(self.binary)
            return _Completed(0)

        with mock.patch.dict(os.environ, {"PATH": elsewhere}, clear=False):
            rust_toolchain.resolve_tui(self.root, runner=runner, printer=lambda *a, **k: None)

        for call in invoked:
            if isinstance(call, list):
                self.assertNotEqual(call[0], os.path.join(elsewhere, "cargo"))
                self.assertNotEqual(call[0], "cargo")
                self.assertNotEqual(call[0], "rustc")

    def test_a_failed_install_resolves_to_nothing_rather_than_a_PATH_cargo(self):
        resolved = rust_toolchain.resolve_tui(
            self.root,
            runner=lambda *a, **k: _Completed(1),
            printer=lambda *a, **k: None,
        )
        self.assertIsNone(resolved)

    def test_a_missing_curl_is_reported_and_not_raised(self):
        messages = []

        def runner(*args, **kwargs):
            raise OSError("curl: not found")

        self.assertFalse(
            rust_toolchain.install_toolchain(self.root, runner=runner, printer=lambda m, **k: messages.append(m))
        )
        self.assertTrue(any("Error installing Rust" in m for m in messages), messages)


class NodoDispatchTests(unittest.TestCase):
    """The CLI must not have kept a path back onto `$PATH`."""

    @staticmethod
    def _source():
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "nodo.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_tui_handler_no_longer_shells_out_to_cargo_run(self):
        source = self._source()
        self.assertNotIn("cargo run", source)
        self.assertIn("resolve_tui", source)

    def test_check_rust_installation_delegates_to_the_resolver(self):
        source = self._source()
        self.assertIn("ensure_toolchain", source)
        # The two probes that made the old function wrong.
        self.assertNotIn("~/.cargo/bin", source)
        self.assertNotIn("'rustc', '--version'", source)


if __name__ == "__main__":
    unittest.main()
