"""The gateway port is assigned only when it has been cleared for use.

The bug these tests pin down: the previous version resolved ``auto`` to a free
port, opened it in the firewall *only if it happened to be root*, and persisted
the number either way. So a single unprivileged ``nodo <anything>`` consumed the
sentinel, and every later daemon start read a plausible port and never opened
anything -- which is exactly how a node ended up accepting services that could
never call back into it.
"""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils.config import AUX_PORT_FILE, ConfigManager
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

    @patch("src.utils.firewall.gateway.assign_gateway_port")
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

    @patch("src.utils.firewall.gateway.assign_gateway_port")
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
            self.assertEqual(mock_open_port.call_args.kwargs["bridge"], "br-ch")
            self.assertEqual(mock_open_port.call_args.kwargs["gateway_ip"], "192.168.200.1")
            self.assertEqual(mock_open_port.call_args.kwargs["subnet"], "192.168.200.0/24")

    @patch(
        "src.utils.firewall.gateway.assign_gateway_port",
        side_effect=GatewayPortUnavailable("blocked", "open it yourself", port=41002),
    )
    @patch("src.utils.config.get_free_port", return_value=41002)
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_port_is_not_persisted_when_it_cannot_be_cleared(
        self, _euid, _free_port, _mock_open_port
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, config_path = self._manager(tmpdir)
            manager.load_config()

            # Sentinel survives, so the next privileged start tries again.
            self.assertIsNone(manager.gateway_port_or_none())
            self.assertIn("GATEWAY_PORT: auto", config_path.read_text(encoding="utf-8"))

    @patch("src.utils.firewall.gateway.assign_gateway_port")
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


class RememberedCandidatePortTests(unittest.TestCase):
    """A blocked candidate is kept, so the operator is asked about ONE port.

    The port is only written to config.yaml once it is usable, so until then every
    run picked a fresh random one -- and every run therefore printed a different
    port to open in the firewall. An operator following the instructions was always
    a step behind: by the time they had opened a port, nodo was asking about
    another. The candidate is parked in ``<CACHE>/aux_port`` and reused instead.
    """

    def setUp(self):
        Singleton._instances.pop(ConfigManager, None)

    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def _manager(self, tmpdir, gateway_port="auto", with_cache=True):
        cache = "\n".join(
            ["main:", f"  STORAGE: {Path(tmpdir) / 'storage'}", "  CACHE: ${main.STORAGE}/__cache__/"]
        ) if with_cache else ""
        config_path = Path(tmpdir) / "config.yaml"
        config_path.write_text(
            f"{cache}\nnetwork:\n  GATEWAY_PORT: {gateway_port}\n"
            "  FREE_PORTS_RANGE:\n    - START: 41000\n      END: 41100\n",
            encoding="utf-8",
        )
        manager = ConfigManager(config_path=str(config_path))
        return manager, config_path

    @staticmethod
    def _aux_path(tmpdir):
        return Path(tmpdir) / "storage" / "__cache__" / AUX_PORT_FILE

    @patch(
        "src.utils.firewall.gateway.assign_gateway_port",
        side_effect=GatewayPortUnavailable("blocked", "open it", port=41000),
    )
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_a_blocked_candidate_is_remembered_in_the_cache(self, _euid, _assign):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, config_path = self._manager(tmpdir)
            manager.load_config()

            # Not assigned -- and not forgotten either.
            self.assertIn("GATEWAY_PORT: auto", config_path.read_text(encoding="utf-8"))
            remembered = int(self._aux_path(tmpdir).read_text(encoding="utf-8"))
            self.assertTrue(41000 <= remembered <= 41100, remembered)

    @patch(
        "src.utils.firewall.gateway.assign_gateway_port",
        side_effect=GatewayPortUnavailable("blocked", "open it", port=41000),
    )
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_the_next_run_asks_about_the_same_port(self, _euid, mock_assign):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir)
            manager.load_config()
            first = mock_assign.call_args.kwargs["port"]

            Singleton._instances.pop(ConfigManager, None)
            manager, _ = self._manager(tmpdir)
            manager.load_config()
            second = mock_assign.call_args.kwargs["port"]

        self.assertEqual(first, second)

    @patch("src.utils.firewall.gateway.assign_gateway_port")
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_the_remembered_candidate_is_the_one_that_gets_assigned(self, _euid, mock_assign):
        with tempfile.TemporaryDirectory() as tmpdir:
            aux = self._aux_path(tmpdir)
            aux.parent.mkdir(parents=True, exist_ok=True)
            aux.write_text("41042\n", encoding="utf-8")

            manager, config_path = self._manager(tmpdir)
            manager.load_config()

            self.assertEqual(mock_assign.call_args.kwargs["port"], 41042)
            self.assertEqual(manager.get_gateway_port(), 41042)

    @patch("src.utils.firewall.gateway.assign_gateway_port")
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_a_candidate_someone_else_now_owns_is_replaced(self, _euid, mock_assign):
        # Reusing a port another process has bound would trade one broken node for
        # another; the point is stability, not stubbornness.
        with tempfile.TemporaryDirectory() as tmpdir:
            with socket.socket() as taken:
                taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                taken.bind(("", 0))
                taken.listen(1)
                port = taken.getsockname()[1]

                aux = self._aux_path(tmpdir)
                aux.parent.mkdir(parents=True, exist_ok=True)
                aux.write_text(f"{port}\n", encoding="utf-8")

                manager, _ = self._manager(tmpdir)
                manager.load_config()

            self.assertNotEqual(mock_assign.call_args.kwargs["port"], port)

    @patch(
        "src.utils.firewall.gateway.assign_gateway_port",
        side_effect=GatewayPortUnavailable("blocked", "open it", port=41000),
    )
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_the_refusal_is_logged_as_a_separated_block(self, _euid, _assign):
        # It is emitted while the config loads -- during nodo.py's imports on a fresh
        # install -- so the KyA banner prints right after it. Unpadded, the two ran
        # together and the instructions were lost in the middle.
        with tempfile.TemporaryDirectory() as tmpdir:
            logged = []
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "network:\n  GATEWAY_PORT: auto\n"
                "  FREE_PORTS_RANGE:\n    - START: 41000\n      END: 41100\n",
                encoding="utf-8",
            )
            ConfigManager(config_path=str(config_path), log=logged.append).load_config()

        message = next(m for m in logged if "not assigned" in m)
        self.assertTrue(message.startswith("\n"), repr(message[:40]))
        self.assertTrue(message.endswith("\n"), repr(message[-40:]))

    @patch("src.utils.config.os.geteuid", return_value=1000)
    def test_the_unprivileged_notice_is_separated_too(self, _euid):
        with tempfile.TemporaryDirectory() as tmpdir:
            logged = []
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "network:\n  GATEWAY_PORT: auto\n  FREE_PORTS_RANGE: []\n", encoding="utf-8"
            )
            ConfigManager(config_path=str(config_path), log=logged.append).load_config()

        message = next(m for m in logged if "not root" in m)
        self.assertTrue(message.startswith("\n") and message.endswith("\n"), repr(message))

    @patch(
        "src.utils.firewall.gateway.assign_gateway_port",
        side_effect=GatewayPortUnavailable("blocked", "open it", port=41000),
    )
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_the_pending_candidate_is_readable_for_diagnostics(self, _euid, mock_assign):
        # `nodo doctor` prints it: with no port assigned, the useful thing to tell an
        # operator is which port to open, not that one will be picked eventually.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, _ = self._manager(tmpdir)
            manager.load_config()

            self.assertIsNone(manager.gateway_port_or_none())
            self.assertEqual(
                manager.pending_gateway_port(), mock_assign.call_args.kwargs["port"]
            )

    @patch("src.utils.firewall.gateway.assign_gateway_port")
    @patch("src.utils.config.os.geteuid", return_value=0)
    def test_an_unusable_cache_path_never_stops_the_assignment(self, _euid, mock_assign):
        # No main.CACHE to resolve: the candidate lands beside config.yaml instead.
        # Remembering it somewhere is what matters, not where.
        with tempfile.TemporaryDirectory() as tmpdir:
            manager, config_path = self._manager(tmpdir, with_cache=False)
            manager.load_config()

            self.assertIsNotNone(manager.gateway_port_or_none())
            self.assertTrue((Path(tmpdir) / AUX_PORT_FILE).exists())


if __name__ == "__main__":
    unittest.main()
