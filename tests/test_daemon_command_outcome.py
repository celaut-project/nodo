"""`nodo daemon` has to report failure through its exit status, not only in print.

The TUI applies a configuration change and the restart that makes the node read it
as one step: it writes `config.yaml`, restarts nodo, and puts the previous file back
if that restart did not happen. That revert is decided on this return value. A
`daemon_command` that printed "requires superuser privileges" and returned success
would have the TUI keep a change the node never loaded, and tell the operator their
node had been restarted onto it.

`status` is the deliberate exception: reporting the state is what it does, and
`systemctl status` exits non-zero for a stopped unit, so forwarding that would make
`nodo daemon status` fail on a node that is merely not running.
"""
import subprocess
import unittest
from unittest import mock

from src.commands.daemon import daemon_command


class DaemonCommandOutcomeTests(unittest.TestCase):

    def _completed(self, returncode: int, stderr: str = ""):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)

    def test_a_restart_without_root_is_reported_as_failure(self):
        with mock.patch("os.geteuid", return_value=1000):
            self.assertFalse(daemon_command(subcommand="restart", main_dir="/nodo"))

    def test_a_restart_that_systemd_refuses_is_reported_as_failure(self):
        with mock.patch("os.geteuid", return_value=0), mock.patch(
            "subprocess.run", return_value=self._completed(1, "Interactive authentication required")
        ):
            self.assertFalse(daemon_command(subcommand="restart", main_dir="/nodo"))

    def test_a_restart_that_fails_to_come_back_up_is_reported_as_failure(self):
        # Stop succeeded, start did not: the node is now down, which is the worst
        # outcome to report as success.
        outcomes = [self._completed(0), self._completed(1, "Job failed")]
        with mock.patch("os.geteuid", return_value=0), mock.patch(
            "subprocess.run", side_effect=outcomes
        ):
            self.assertFalse(daemon_command(subcommand="restart", main_dir="/nodo"))

    def test_a_restart_that_worked_is_reported_as_success(self):
        with mock.patch("os.geteuid", return_value=0), mock.patch(
            "subprocess.run", return_value=self._completed(0)
        ):
            self.assertTrue(daemon_command(subcommand="restart", main_dir="/nodo"))

    def test_start_and_stop_forward_their_outcome(self):
        for subcommand in ("start", "stop"):
            with self.subTest(subcommand=subcommand):
                with mock.patch("os.geteuid", return_value=0), mock.patch(
                    "subprocess.run", return_value=self._completed(0)
                ):
                    self.assertTrue(daemon_command(subcommand=subcommand, main_dir="/nodo"))
                with mock.patch("os.geteuid", return_value=0), mock.patch(
                    "subprocess.run", return_value=self._completed(5, "boom")
                ):
                    self.assertFalse(daemon_command(subcommand=subcommand, main_dir="/nodo"))

    def test_status_succeeds_even_when_the_unit_is_stopped(self):
        with mock.patch("os.geteuid", return_value=0), mock.patch(
            "subprocess.run", return_value=self._completed(3)
        ):
            self.assertTrue(daemon_command(subcommand="status", main_dir="/nodo"))

    def test_an_unknown_subcommand_is_a_failure(self):
        with mock.patch("os.geteuid", return_value=0):
            self.assertFalse(daemon_command(subcommand="frobnicate", main_dir="/nodo"))
            self.assertFalse(daemon_command(subcommand=None, main_dir="/nodo"))


if __name__ == "__main__":
    unittest.main()
