"""Why editing a config value from the TUI sometimes needs root, and sometimes not.

The report was that the ENERGY page's kWh price asked for sudo "and the rest did
not". It does not, and neither does anything else: there is exactly one writer.
`energy.PRICE_PER_KWH` goes through `App::write_config_value` like every price,
every CELL lever and every raw Config row, into the one `apply_config_change`
transaction.

What needs root is the **restart** that transaction owes a *serving* node. The
editor's invariant is that what config.yaml says is what the running node loaded,
so a write is followed by `nodo daemon restart` and undone if the node does not
come back -- and `daemon_command` refuses outright under a non-zero euid. An
unprivileged edit therefore *writes*, fails the restart, and is reverted, which
from the operator's chair is indistinguishable from "this setting needs sudo".

The asymmetry that made it look arbitrary is that the same edit against a
**stopped** node owes no restart and lands silently.

These tests pin the two facts that root cause rests on, so a future change that
moves the privileged step cannot quietly invalidate the warning the TUI now shows:

1. config.yaml itself is writable by an ordinary user -- the file is not the
   obstacle.
2. `nodo daemon restart` is the step that refuses without root.
"""

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TheFileIsNotTheObstacle(unittest.TestCase):
    """config.yaml is deliberately writable by the installing user.

    `install.sh` does `chmod a+w` on it at creation, and every save through
    `ConfigManager._atomic_write` re-applies `0o666` -- explicitly so that
    `sudo nodo update` and an ordinary user can both write it. A permissions theory
    of the sudo prompt has to survive this, and does not.
    """

    def test_install_sh_makes_the_config_world_writable(self):
        with open(os.path.join(_ROOT, "install.sh"), "r") as handle:
            install = handle.read()

        self.assertIn('chmod a+w "$TARGET_DIR/config.yaml"', install)

    def test_every_save_re_applies_a_world_writable_mode(self):
        """Not just at install: a save must not quietly tighten the file.

        `_atomic_write` replaces config.yaml with a fresh temp file, which is created
        `0600` by `mkstemp`. Without the chmod, the first write by root would leave
        an unprivileged operator unable to edit anything -- and *that* would be a
        real per-file sudo requirement.
        """
        from src.utils.config import ConfigManager

        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yaml")
            with open(config_path, "w") as handle:
                handle.write("energy:\n  PRICE_PER_KWH: 0.0\n")

            manager = ConfigManager.__new__(ConfigManager)
            manager.config_path = config_path
            written = manager._atomic_write({"energy": {"PRICE_PER_KWH": 0.25}})

            self.assertTrue(written)
            mode = stat.S_IMODE(os.stat(config_path).st_mode)
            self.assertTrue(
                mode & stat.S_IWOTH,
                f"config.yaml came back as {oct(mode)}, which an ordinary user cannot edit",
            )

    def test_the_write_lands_without_any_privileged_call(self):
        """The YAML write itself spawns nothing and asks nobody.

        If a config write needed root, this is where it would show: a `sudo`, a
        `chown`, a `systemctl`. There is none, which is what leaves the restart as
        the only candidate.
        """
        from src.utils.config import ConfigManager

        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yaml")
            with open(config_path, "w") as handle:
                handle.write("energy:\n  PRICE_PER_KWH: 0.0\n")

            manager = ConfigManager.__new__(ConfigManager)
            manager.config_path = config_path

            with mock.patch(
                "subprocess.run", side_effect=AssertionError("a config write spawned a process")
            ):
                manager._atomic_write({"energy": {"PRICE_PER_KWH": 0.25}})

            with open(config_path, "r") as handle:
                self.assertIn("0.25", handle.read())


class TheRestartIsTheObstacle(unittest.TestCase):
    """`nodo daemon restart` is the privileged step, and the only one.

    This is what the TUI's new warning reports, so it has to stay true: a change
    that moved the privilege elsewhere would leave the interface confidently naming
    the wrong cause.
    """

    def test_daemon_command_refuses_without_root(self):
        from src.commands.daemon import daemon_command

        with mock.patch("os.geteuid", return_value=1000), mock.patch(
            "subprocess.run", side_effect=AssertionError("it tried to drive systemd anyway")
        ):
            self.assertFalse(daemon_command(subcommand="restart", main_dir=_ROOT))

    def test_the_refusal_happens_before_any_subcommand_is_looked_at(self):
        """Every subcommand, not just restart: the euid check is the first thing.

        Worth pinning because the TUI's warning says "the restart needs root"
        without qualifying which flavour of it -- and `apply_config_change` calls
        `nodo daemon restart` specifically.
        """
        from src.commands.daemon import daemon_command

        for subcommand in ("start", "stop", "restart"):
            with mock.patch("os.geteuid", return_value=1000), mock.patch(
                "subprocess.run", side_effect=AssertionError("drove systemd without root")
            ):
                self.assertFalse(
                    daemon_command(subcommand=subcommand, main_dir=_ROOT),
                    f"{subcommand} did not refuse",
                )

    def test_a_stopped_node_is_owed_no_restart_at_all(self):
        """The asymmetry that made the requirement look arbitrary.

        `is_serving()` is what decides whether a write owes a restart. With nothing
        on the gateway port there is no running node to disagree with the file, so
        the change simply stands -- and the same edit that was refused a moment ago
        succeeds with no privileges whatsoever.
        """
        from src.commands.daemon import is_serving

        manager = mock.Mock()
        manager.gateway_port_or_none.return_value = None
        with mock.patch("src.utils.config.ConfigManager", return_value=manager):
            self.assertFalse(is_serving())

    def test_restart_after_config_write_says_so_rather_than_failing(self):
        """And the CLI's own equivalent path already words it this way.

        The TUI's warning is phrased to match: the file is fine, the next start
        reads it, and the only thing missing is the restart.
        """
        from src.commands.daemon import restart_after_config_write

        with mock.patch("src.commands.daemon.config_digest", return_value="after"), \
             mock.patch("src.commands.daemon.is_serving", return_value=False), \
             mock.patch("src.commands.daemon.daemon_command") as daemon:
            self.assertTrue(restart_after_config_write("before"))

        daemon.assert_not_called()


class TheTuiNamesTheRealCause(unittest.TestCase):
    """The warning shown in the editor points at the restart, not at the key.

    Read off the Rust source: there is no interpreter to run it from here, and the
    thing being pinned is that the explanation has not drifted back to "this setting
    needs sudo", which is the misdiagnosis the whole investigation was about.
    """

    def _app_rs(self):
        with open(
            os.path.join(_ROOT, "src", "commands", "tui", "src", "app.rs"), "r"
        ) as handle:
            return handle.read()

    def test_the_hint_is_conditioned_on_the_node_serving(self):
        source = self._app_rs()

        hint = source.split("fn config_write_needs_root", 1)[1].split("}", 1)[0]
        self.assertIn("service_status", hint)
        self.assertIn("running", hint)

    def test_the_hint_names_the_restart(self):
        source = self._app_rs()

        hint = source.split("fn config_write_root_hint", 1)[1].split("\n    }", 1)[0]
        self.assertIn("daemon restart", hint)
        # And not a claim about this particular key, which is the thing that was
        # wrong with the operator's original theory and would be wrong here too.
        self.assertNotIn("PRICE_PER_KWH", hint)


if __name__ == "__main__":
    unittest.main()
