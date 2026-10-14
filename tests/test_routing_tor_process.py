"""The Tor process nodo manages: when it starts, what it is given, how it fails."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.commands import tor as tor_command
    from src.routing import tor as tor_module
    from src.routing import tor_install
    from src.utils.config_validation import ConfigValidationError, validate_routing_config
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

FAKE_TOR = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "Tor version {version}."; exit 0; fi
cat "$2" > "{seen}"
echo "Bootstrapped 100% (done): Done"
exec sleep 30
"""


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TorProcessTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.settings = {"STORAGE": str(self.dir / "storage"), "routing.tor.ENABLED": "auto",
                         "routing.tor.BINARY": "", "dependencies.tor.RUNTIME_ROOT": str(self.dir / "runtime")}
        for module in (tor_module, tor_install):
            patcher = mock.patch.object(
                module.env_manager, "get",
                side_effect=lambda key, default=None: self.settings.get(key, default),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_tor(self, version="0.4.9.14"):
        path = self.dir / "fake-tor"
        self.seen = self.dir / "torrc-seen"
        path.write_text(FAKE_TOR.format(version=version, seen=self.seen))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.settings["routing.tor.BINARY"] = str(path)
        return path

    def _backend(self):
        backend = tor_module.TorBackend()
        self.addCleanup(backend.stop)
        return backend

    def test_auto_without_an_installed_tor_does_nothing_and_installs_nothing(self):
        backend = self._backend()
        with mock.patch.object(tor_module, "ensure_tor_installed") as install:
            backend.start()
        install.assert_not_called()
        self.assertEqual(backend.status()[0], "not-installed")
        self.assertFalse(backend.available())

    def test_false_never_starts(self):
        self.settings["routing.tor.ENABLED"] = False
        self._fake_tor()
        backend = self._backend()
        backend.start()
        self.assertEqual(backend.status()[0], "disabled")

    def test_true_installs_a_missing_tor_and_a_failed_install_is_a_failed_state(self):
        self.settings["routing.tor.ENABLED"] = True
        backend = self._backend()
        with mock.patch.object(
            tor_module, "ensure_tor_installed",
            side_effect=tor_module.TorInstallError("no compiler"),
        ) as install:
            backend.start()
        install.assert_called_once()
        self.assertEqual(backend.status(), ("failed", "no compiler"))

    def test_it_runs_tor_with_a_loopback_only_http_tunnel_and_becomes_ready(self):
        self._fake_tor()
        backend = self._backend()
        backend.start()
        self.assertTrue(wait_for(backend.available), backend.status())
        torrc = self.seen.read_text()
        self.assertIn(f"HTTPTunnelPort 127.0.0.1:{backend._http_port}", torrc)
        self.assertIn("SocksPort 0", torrc)
        self.assertNotIn("ControlPort", torrc)
        self.assertEqual(oct((self.dir / "storage" / "tor" / "torrc").stat().st_mode & 0o777), "0o600")
        state = json.loads((self.dir / "storage" / "tor" / "state.json").read_text())
        self.assertEqual((state["state"], state["pid"]), ("ready", os.getpid()))

    def test_without_an_onion_service_tor_hosts_nothing(self):
        self._fake_tor()
        backend = self._backend()
        backend.start()
        self.assertTrue(wait_for(backend.available))
        self.assertNotIn("HiddenService", self.seen.read_text())
        self.assertFalse((self.dir / "storage" / "tor" / "gateway").exists())

    def test_the_onion_service_forwards_to_loopback_and_uses_the_derived_key(self):
        self._fake_tor()
        backend = self._backend()
        mnemonic = ("abandon abandon abandon abandon abandon abandon abandon abandon "
                    "abandon abandon abandon about")
        host = backend.configure_onion(mnemonic, local_port=4321, virtual_port=8090)
        backend.start()
        self.assertTrue(wait_for(backend.available), backend.status())
        torrc = self.seen.read_text()
        service_dir = self.dir / "storage" / "tor" / "gateway"
        self.assertIn(f"HiddenServiceDir {service_dir}", torrc)
        self.assertIn("HiddenServiceVersion 3", torrc)
        self.assertIn("HiddenServicePort 8090 127.0.0.1:4321", torrc)
        self.assertEqual((service_dir / "hostname").read_text().strip(), host)
        self.assertEqual((backend.onion_host(), backend.onion_port()), (host, 8090))
        state = json.loads((self.dir / "storage" / "tor" / "state.json").read_text())
        self.assertEqual(state["onion"], host)

    def test_a_tor_older_than_0_4_8_is_refused(self):
        self._fake_tor(version="0.4.7.16")
        backend = self._backend()
        backend.start()
        state, detail = backend.status()
        self.assertEqual(state, "failed")
        self.assertIn("0.4.8", detail)
        self.assertFalse(backend.available())

    def test_stop_ends_the_process(self):
        self._fake_tor()
        backend = self._backend()
        backend.start()
        self.assertTrue(wait_for(backend.available))
        process = backend._process
        backend.stop()
        self.assertIsNotNone(process.poll())
        self.assertFalse(backend.available())

    def test_a_tor_that_exits_is_not_available(self):
        path = self.dir / "dying-tor"
        path.write_text('#!/bin/sh\n[ "$1" = "--version" ] && { echo "Tor version 0.4.9.14."; exit 0; }\nexit 3\n')
        path.chmod(0o755)
        self.settings["routing.tor.BINARY"] = str(path)
        backend = self._backend()
        backend.start()
        self.assertTrue(wait_for(lambda: backend.status()[0] == "failed"), backend.status())
        self.assertFalse(backend.available())

    def test_a_missing_binary_override_is_a_failed_state_and_never_installs(self):
        self.settings["routing.tor.ENABLED"] = True
        self.settings["routing.tor.BINARY"] = str(self.dir / "nowhere")
        backend = self._backend()
        with mock.patch.object(tor_module, "ensure_tor_installed") as install:
            backend.start()
        install.assert_not_called()
        self.assertEqual(backend.status()[0], "failed")

    def test_the_mode_reads_true_false_and_auto(self):
        for value, expected in ((True, "on"), (False, "off"), ("auto", "auto"), (None, "auto")):
            self.settings["routing.tor.ENABLED"] = value
            self.assertEqual(tor_module.tor_mode(), expected)

    def test_status_reports_the_node_only_while_its_process_lives(self):
        self._fake_tor()
        state_file = self.dir / "storage" / "tor" / "state.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps({"state": "ready", "detail": "", "pid": os.getpid()}))
        self.assertEqual(tor_command.status()["node"], "ready")
        state_file.write_text(json.dumps({"state": "ready", "detail": "", "pid": 2 ** 22 + 1}))
        self.assertEqual(tor_command.status()["node"], "not running")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RoutingConfigTests(unittest.TestCase):
    def test_defaults_and_missing_sections_are_valid(self):
        validate_routing_config({})
        validate_routing_config({"routing": {}})
        validate_routing_config({"routing": {"tor": {"ENABLED": "auto", "BINARY": ""}}})
        validate_routing_config({"routing": {"tor": {"ENABLED": True}}})
        validate_routing_config({"routing": {"tor": {"ENABLED": False}}})

    def test_a_mistyped_setting_stops_the_node(self):
        for bad in (
            {"routing": []},
            {"routing": {"tor": "yes"}},
            {"routing": {"tor": {"ENABLED": "yes"}}},
            {"routing": {"tor": {"ENABLED": 1}}},
            {"routing": {"tor": {"BINARY": 5}}},
        ):
            with self.assertRaises(ConfigValidationError, msg=bad):
                validate_routing_config(bad)

    def test_the_shipped_example_config_is_valid(self):
        import yaml
        example = Path(__file__).resolve().parent.parent / "config.example.yaml"
        config = yaml.safe_load(example.read_text())
        validate_routing_config(config)
        self.assertEqual(config["routing"]["tor"]["ENABLED"], "auto")
        self.assertIn("RUNTIME_ROOT", config["dependencies"]["tor"])


if __name__ == "__main__":
    unittest.main()
