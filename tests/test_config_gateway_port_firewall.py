import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils.config import ConfigManager
from src.utils.singleton import Singleton


class ConfigGatewayPortFirewallTests(unittest.TestCase):
    def setUp(self):
        Singleton._instances.pop(ConfigManager, None)

    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def _write_config(self, config_path: Path):
        config_path.write_text(
            "network:\n"
            "  GATEWAY_PORT: auto\n"
            "  FREE_PORTS_RANGE: []\n",
            encoding="utf-8",
        )

    @patch("src.utils.config.get_free_port", return_value=41000)
    @patch("src.utils.config.os.geteuid", return_value=0)
    @patch("src.utils.config.subprocess.run")
    def test_auto_gateway_port_uses_iptables_insert_when_rule_missing(
        self,
        mock_run,
        _mock_geteuid,
        _mock_get_free_port,
    ):
        mock_run.side_effect = [
            unittest.mock.Mock(returncode=1, stderr=""),
            unittest.mock.Mock(returncode=0, stderr="", stdout=""),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            self._write_config(config_path)

            manager = ConfigManager(config_path=str(config_path))
            manager.load_config()

            self.assertEqual(manager.get("network.GATEWAY_PORT"), 41000)

        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(
            mock_run.call_args_list[0].args[0][:3],
            ["iptables", "-C", "INPUT"],
        )
        self.assertEqual(
            mock_run.call_args_list[1].args[0][:3],
            ["iptables", "-I", "INPUT"],
        )

    @patch("src.utils.config.get_free_port", return_value=41001)
    @patch("src.utils.config.os.geteuid", return_value=0)
    @patch("src.utils.config.subprocess.run")
    def test_auto_gateway_port_skips_insert_when_rule_exists(
        self,
        mock_run,
        _mock_geteuid,
        _mock_get_free_port,
    ):
        mock_run.return_value = unittest.mock.Mock(returncode=0, stderr="", stdout="")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            self._write_config(config_path)

            manager = ConfigManager(config_path=str(config_path))
            manager.load_config()

            self.assertEqual(manager.get("network.GATEWAY_PORT"), 41001)

        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(mock_run.call_args_list[0].args[0][:3], ["iptables", "-C", "INPUT"])

    @patch("src.utils.config.get_free_port", return_value=41002)
    @patch("src.utils.config.os.geteuid", return_value=0)
    @patch("src.utils.config.subprocess.run", side_effect=FileNotFoundError)
    def test_auto_gateway_port_raises_when_iptables_missing(
        self,
        _mock_run,
        _mock_geteuid,
        _mock_get_free_port,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            self._write_config(config_path)

            manager = ConfigManager(config_path=str(config_path))
            with self.assertRaises(Exception) as ctx:
                manager.load_config()

        self.assertIn("iptables command not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
