"""The gateway port is assigned only when it can actually be opened.

The bug these tests pin down: the previous version resolved ``auto`` to a free
port, opened it in the firewall *only if it happened to be root*, and persisted
the number either way. So a single unprivileged ``nodo <anything>`` consumed the
sentinel, and every later daemon start read a plausible port and never opened
anything -- which is exactly how a node ended up accepting services that could
never call back into it.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils.config import ConfigManager
from src.utils.firewall.gateway import GatewayPortUnavailable
from src.utils.singleton import Singleton


class GatewayPortResolutionTests(unittest.TestCase):
    def setUp(self):
        Singleton._instances.pop(ConfigManager, None)

    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def _write_config(self, config_path: Path, gateway_port: str = "auto"):
        config_path.write_text(
            "network:\n"
            f"  GATEWAY_PORT: {gateway_port}\n"
            "  FREE_PORTS_RANGE: []\n",
            encoding="utf-8",
        )

    def _manager(self, tmpdir, gateway_port="auto"):
        config_path = Path(tmpdir) / "config.yaml"
        self._write_config(config_path, gateway_port=gateway_port)
        return ConfigManager(config_path=str(config_path)), config_path

    @patch("src.utils.firewall.gateway.ensure_gateway_port_open")
    @patch("src.utils.config.get_free_port", return_value=41000)
    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_unprivileged_run_leaves_the_sentinel_alone(
        self, _euid, mock_free_port, mock_open_port
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, config_path = self._manager(tmpdir)
            manager.load_config()

            self.assertIsNone(manager.gateway_port_or_none())
            # Neither picked nor opened, and nothing written back.
            mock_free_port.assert_not_called()
            mock_open_port.assert_not_called()
            self.assertIn("GATEWAY_PORT: auto", config_path.read_text(encoding="utf-8"))

    @patch("src.utils.firewall.gateway.ensure_gateway_port_open")
    @patch("src.utils.config.get_free_port", return_value=41001)
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_privileged_run_opens_then_persists(self, _euid, _free_port, mock_open_port):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, config_path = self._manager(tmpdir)
            manager.load_config()

            self.assertEqual(manager.get_gateway_port(), 41001)
            self.assertIn("GATEWAY_PORT: 41001", config_path.read_text(encoding="utf-8"))

            mock_open_port.assert_called_once()
            self.assertEqual(mock_open_port.call_args.kwargs["port"], 41001)
            # The probe must run before the port is committed to the file.
            self.assertEqual(mock_open_port.call_args.kwargs["bridge"], "br-ch")
            self.assertEqual(mock_open_port.call_args.kwargs["subnet"], "192.168.200.0/24")

    @patch(
        "src.utils.firewall.gateway.ensure_gateway_port_open",
        side_effect=GatewayPortUnavailable("blocked", "open it yourself", port=41002),
    )
    @patch("src.utils.config.get_free_port", return_value=41002)
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_port_is_not_persisted_when_it_cannot_be_opened(
        self, _euid, _free_port, _mock_open_port
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, config_path = self._manager(tmpdir)
            manager.load_config()

            # Sentinel survives, so the next privileged start tries again.
            self.assertIsNone(manager.gateway_port_or_none())
            self.assertIn("GATEWAY_PORT: auto", config_path.read_text(encoding="utf-8"))

    @patch("src.utils.firewall.gateway.ensure_gateway_port_open")
    @patch("src.utils.config.get_free_port")
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_an_already_assigned_port_is_never_reassigned(
        self, _euid, mock_free_port, mock_open_port
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir, gateway_port="58443")
            manager.load_config()

            self.assertEqual(manager.get_gateway_port(), 58443)
            mock_free_port.assert_not_called()
            mock_open_port.assert_not_called()


class GatewayPortAccessorTests(unittest.TestCase):
    """``auto`` must never reach a caller: no port is an exception, not a value."""

    def setUp(self):
        Singleton._instances.pop(ConfigManager, None)

    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def _manager(self, tmpdir, gateway_port):
        config_path = Path(tmpdir) / "config.yaml"
        config_path.write_text(
            f"network:\n  GATEWAY_PORT: {gateway_port}\n  FREE_PORTS_RANGE: []\n",
            encoding="utf-8",
        )
        return ConfigManager(config_path=str(config_path))

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_get_raises_instead_of_returning_the_sentinel(self, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir, "auto")
            for key in ("GATEWAY_PORT", "network.GATEWAY_PORT"):
                with self.subTest(key=key):
                    with self.assertRaises(GatewayPortUnavailable) as ctx:
                        manager.get(key)
                    # The message has to tell an operator what to do next.
                    self.assertIn("sudo nodo serve", ctx.exception.instructions)

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_or_none_reports_the_absence_without_raising(self, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir, "auto")
            self.assertIsNone(manager.gateway_port_or_none())

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_assigned_port_comes_back_as_an_int(self, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir, "58443")
            self.assertEqual(manager.get("GATEWAY_PORT"), 58443)
            self.assertEqual(manager.get("network.GATEWAY_PORT"), 58443)
            self.assertEqual(manager.get_gateway_port(), 58443)

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_an_unusable_value_is_treated_as_unassigned(self, _euid):
        for bad in ('"not-a-port"', "0", "70000", '""'):
            with self.subTest(value=bad):
                Singleton._instances.pop(ConfigManager, None)
                with tempfile.TemporaryDirectory() as tmpdir:
                    manager = self._manager(tmpdir, bad)
                    self.assertIsNone(manager.gateway_port_or_none())
                    with self.assertRaises(GatewayPortUnavailable):
                        manager.get_gateway_port()


if __name__ == "__main__":
    unittest.main()
