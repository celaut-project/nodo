import tempfile
import unittest
from pathlib import Path

import yaml

from src.utils import config as config_module
from src.utils.config import ConfigManager
from src.utils.singleton import Singleton

PROOF_ID = "46bf6503dfa0551e7a74f005f33b717f26115ed21f338297639040d3d0cfe484"

# The layout the bug was reported on: `nodo serve` boots with an empty proof id,
# then the CLI submits a proof and writes it while the daemon keeps running.
BASE_CONFIG = (
    "ledgers:\n"
    "  ergo:\n"
    "    NODE_URL: http://node.example:9053\n"
    "    reputation:\n"
    "      REPUTATION_PROOF_ID: ''\n"
    "network:\n"
    "  GATEWAY_PORT: 4040\n"
)


class ExternalConfigChangeTests(unittest.TestCase):
    """A second process writing config.yaml must not be ignored nor overwritten."""

    def setUp(self):
        Singleton._instances.pop(ConfigManager, None)

    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def _manager(self, config_path: Path) -> ConfigManager:
        manager = ConfigManager(config_path=str(config_path))
        manager.load_config()
        return manager

    @staticmethod
    def _write_externally(config_path: Path, key_path, value):
        """Rewrite the file the way another nodo process would."""
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        target = data
        for key in key_path[:-1]:
            target = target[key]
        target[key_path[-1]] = value
        config_path.write_text(yaml.safe_dump(data, indent=2), encoding="utf-8")

    def test_get_picks_up_an_external_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            self.assertEqual(
                manager.get("ledgers.ergo.reputation.REPUTATION_PROOF_ID"), ""
            )

            self._write_externally(
                config_path, ["ledgers", "ergo", "reputation", "REPUTATION_PROOF_ID"], PROOF_ID
            )
            # Beat the debounce, as a long-running daemon always would.
            manager._last_reload_check = 0.0

            self.assertEqual(
                manager.get("ledgers.ergo.reputation.REPUTATION_PROOF_ID"), PROOF_ID
            )

    def test_reload_is_debounced_between_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            manager.get("network.GATEWAY_PORT")  # Arms the debounce window.

            self._write_externally(
                config_path, ["ledgers", "ergo", "reputation", "REPUTATION_PROOF_ID"], PROOF_ID
            )

            # Within _RELOAD_CHECK_INTERVAL the file is not re-read.
            self.assertEqual(
                manager.get("ledgers.ergo.reputation.REPUTATION_PROOF_ID"), ""
            )

    def test_set_does_not_clobber_an_external_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)

            self._write_externally(
                config_path, ["ledgers", "ergo", "reputation", "REPUTATION_PROOF_ID"], PROOF_ID
            )

            # The daemon rotating the Ergo node URL rewrites the whole file; the
            # proof id it never saw must survive. No debounce reset here: set()
            # forces the freshness check itself.
            manager.set("ledgers.ergo.NODE_URL", "http://other.example:9053")

            on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                on_disk["ledgers"]["ergo"]["reputation"]["REPUTATION_PROOF_ID"], PROOF_ID
            )
            self.assertEqual(
                on_disk["ledgers"]["ergo"]["NODE_URL"], "http://other.example:9053"
            )

    def test_own_write_does_not_trigger_a_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            manager.set("ledgers.ergo.reputation.REPUTATION_PROOF_ID", PROOF_ID)
            manager._last_reload_check = 0.0

            self.assertEqual(
                manager.get("ledgers.ergo.reputation.REPUTATION_PROOF_ID"), PROOF_ID
            )

    def test_unreadable_file_keeps_the_loaded_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)

            # Whatever the reason (truncated mid-write, hand-edited into invalid
            # YAML), a bad file must not wipe what the process is running on.
            config_path.write_text("", encoding="utf-8")
            manager._last_reload_check = 0.0

            self.assertEqual(manager.get("network.GATEWAY_PORT"), 4040)

    def test_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            manager.set("network.GATEWAY_PORT", 5050)

            # No leftover temporary files, and a reader only ever sees whole YAML.
            self.assertEqual([p.name for p in Path(tmpdir).iterdir()], ["config.yaml"])
            self.assertEqual(
                yaml.safe_load(config_path.read_text(encoding="utf-8"))["network"]["GATEWAY_PORT"],
                5050,
            )

    def test_falls_back_to_in_place_write_when_the_directory_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            original_atomic_write = manager._atomic_write
            manager._atomic_write = lambda safe_config: False
            try:
                manager.set("network.GATEWAY_PORT", 6060)
            finally:
                manager._atomic_write = original_atomic_write

            self.assertEqual(
                yaml.safe_load(config_path.read_text(encoding="utf-8"))["network"]["GATEWAY_PORT"],
                6060,
            )

    def test_reload_interval_is_a_constant(self):
        # Deliberately not configurable: a knob here only adds a way to get the
        # daemon back into the stale-config state.
        self.assertIsInstance(config_module._RELOAD_CHECK_INTERVAL, float)


if __name__ == "__main__":
    unittest.main()
