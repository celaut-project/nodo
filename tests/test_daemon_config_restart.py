import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.commands import daemon
from src.utils.config import ConfigManager
from src.utils.singleton import Singleton

BASE_CONFIG = "network:\n  GATEWAY_PORT: 4040\n"


class RestartAfterConfigWriteTests(unittest.TestCase):
    """A CLI command that writes config.yaml has to restart the node it wrote for.

    Only the daemon's own writes are already live in the process that made them.
    Anything written from a separate process -- `nodo sync_reputation_proof`,
    `nodo submit_reputation` -- lands in a file the running node will not re-read,
    and which it overwrites from what it loaded the next time it persists a value of
    its own. The restart is what makes such a write real.
    """

    def setUp(self):
        Singleton._instances.pop(ConfigManager, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp.name) / "config.yaml"
        self.config_path.write_text(BASE_CONFIG, encoding="utf-8")
        ConfigManager(config_path=str(self.config_path))

    def tearDown(self):
        self._tmp.cleanup()
        Singleton._instances.pop(ConfigManager, None)

    def _write(self):
        self.config_path.write_text(BASE_CONFIG + "changed: true\n", encoding="utf-8")

    def test_a_command_that_wrote_nothing_restarts_nothing(self):
        before = daemon.config_digest()

        with mock.patch.object(daemon, "is_serving", return_value=True), \
                mock.patch.object(daemon, "daemon_command") as restart:
            self.assertTrue(daemon.restart_after_config_write(before))

        restart.assert_not_called()

    def test_a_write_with_nothing_serving_is_left_for_the_next_start(self):
        before = daemon.config_digest()
        self._write()

        with mock.patch.object(daemon, "is_serving", return_value=False), \
                mock.patch.object(daemon, "daemon_command") as restart:
            self.assertTrue(daemon.restart_after_config_write(before))

        restart.assert_not_called()

    def test_a_write_on_a_serving_node_restarts_it(self):
        before = daemon.config_digest()
        self._write()

        with mock.patch.object(daemon, "is_serving", return_value=True), \
                mock.patch.object(daemon, "daemon_command", return_value=True) as restart:
            self.assertTrue(daemon.restart_after_config_write(before))

        restart.assert_called_once_with("restart", None)

    def test_a_restart_that_did_not_happen_is_reported_as_failure(self):
        # Not silently: the operator is the only one who can finish the job, and
        # until they do the write is both invisible and doomed.
        before = daemon.config_digest()
        self._write()

        with mock.patch.object(daemon, "is_serving", return_value=True), \
                mock.patch.object(daemon, "daemon_command", return_value=False):
            self.assertFalse(daemon.restart_after_config_write(before))

    def test_an_unreadable_config_restarts_nothing(self):
        before = daemon.config_digest()
        self.config_path.unlink()

        with mock.patch.object(daemon, "is_serving", return_value=True), \
                mock.patch.object(daemon, "daemon_command") as restart:
            self.assertTrue(daemon.restart_after_config_write(before))

        restart.assert_not_called()

    def test_the_digest_follows_the_file(self):
        before = daemon.config_digest()
        self.assertIsNotNone(before)
        self._write()
        self.assertNotEqual(daemon.config_digest(), before)


class IsServingTests(unittest.TestCase):
    def setUp(self):
        Singleton._instances.pop(ConfigManager, None)

    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def test_no_assigned_port_means_nothing_is_serving(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")
            manager = ConfigManager(config_path=str(config_path))
            with mock.patch.object(manager, "gateway_port_or_none", return_value=None):
                self.assertFalse(daemon.is_serving())


if __name__ == "__main__":
    unittest.main()
