import re
import tempfile
import unittest
from pathlib import Path

import yaml

from src.utils.config import CONFIG_BACKUP_RETENTION, ConfigManager, backup_config_file
from src.utils.singleton import Singleton

PROOF_ID = "46bf6503dfa0551e7a74f005f33b717f26115ed21f338297639040d3d0cfe484"

BASE_CONFIG = (
    "ledgers:\n"
    "  ergo:\n"
    "    NODE_URL: http://node.example:9053\n"
    "    reputation:\n"
    "      REPUTATION_PROOF_ID: ''\n"
    "network:\n"
    "  GATEWAY_PORT: 4040\n"
)


class LoadedConfigIsTheRunningConfigTests(unittest.TestCase):
    """config.yaml is read once per process; the file is not watched.

    A running node is the configuration it booted with. Everything derived from a
    config value -- the identity keypair, the TLS certificate served to peers, an
    interpolated path -- is therefore stable for the life of the process, which is
    what makes caching any of it correct (issue #310). An edit reaches the node
    through a restart, and `nodo tui` is the supported way to make that one step.
    """

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
        """Rewrite the file the way an editor or another nodo process would."""
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        target = data
        for key in key_path[:-1]:
            target = target[key]
        target[key_path[-1]] = value
        config_path.write_text(yaml.safe_dump(data, indent=2), encoding="utf-8")

    def test_get_ignores_an_external_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            self._write_externally(
                config_path, ["ledgers", "ergo", "reputation", "REPUTATION_PROOF_ID"], PROOF_ID
            )

            self.assertEqual(
                manager.get("ledgers.ergo.reputation.REPUTATION_PROOF_ID"), ""
            )

    def test_the_identity_mnemonic_cannot_change_under_a_running_process(self):
        # The one that mattered: a rotated identity that the process picked up while
        # still serving the certificate minted from the old one left the node
        # announcing a key it could not prove (issue #310).
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            booted_with = manager.get("identity.MNEMONIC")
            self.assertTrue(booted_with)

            self._write_externally(
                config_path, ["identity", "MNEMONIC"], "abandon abandon ability"
            )

            self.assertEqual(manager.get("identity.MNEMONIC"), booted_with)

    def test_a_truncated_file_cannot_wipe_the_running_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            config_path.write_text("", encoding="utf-8")

            self.assertEqual(manager.get("network.GATEWAY_PORT"), 4040)
            self.assertEqual(
                manager.get("ledgers.ergo.NODE_URL"), "http://node.example:9053"
            )

    def test_set_writes_the_loaded_config_over_the_file(self):
        # Saving rewrites the whole file from what this process holds, so a key
        # written behind its back is not preserved. Stated as a test because it is
        # the price of never re-reading, not an accident.
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            self._write_externally(
                config_path, ["ledgers", "ergo", "reputation", "REPUTATION_PROOF_ID"], PROOF_ID
            )

            manager.set("ledgers.ergo.NODE_URL", "http://other.example:9053")

            on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                on_disk["ledgers"]["ergo"]["reputation"]["REPUTATION_PROOF_ID"], ""
            )
            self.assertEqual(
                on_disk["ledgers"]["ergo"]["NODE_URL"], "http://other.example:9053"
            )

    def test_own_write_is_visible_in_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            manager.set("ledgers.ergo.reputation.REPUTATION_PROOF_ID", PROOF_ID)

            self.assertEqual(
                manager.get("ledgers.ergo.reputation.REPUTATION_PROOF_ID"), PROOF_ID
            )

    def test_an_identity_mnemonic_is_generated_when_the_file_has_none(self):
        # A node without one has no name and cannot serve or dial, so it is never left
        # unset -- and it is its own section, not a key inside any ledger's.
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            identity = manager.get("identity.MNEMONIC")
            wallet = manager.get("ledgers.ergo.WALLET_MNEMONIC")

            self.assertTrue(identity)
            self.assertTrue(wallet)
            self.assertNotEqual(identity, wallet)

    def test_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            manager.set("network.GATEWAY_PORT", 5050)

            # No leftover atomic-write temp files -- mkstemp writes a dotted
            # ".config-*.yaml" scratch file and os.replace should consume it -- and a
            # reader only ever sees whole YAML.
            names = sorted(p.name for p in Path(tmpdir).iterdir())
            self.assertEqual(
                [n for n in names if n.startswith(".config-")], [],
                f"atomic-write temp file left behind: {names}",
            )
            # One timestamped backup per write (issue #255): the load that minted the
            # mnemonics saved once, and the set() above saved again. Both land inside
            # the same second and both survive, which is what the random tail on
            # config-<YYYYMMDDHHMMSS>-<nnnn>.yaml is for.
            self.assertEqual(
                [n for n in names if re.fullmatch(r"config-\d{14}-\d{4}\.yaml", n)].__len__(),
                2, names,
            )
            self.assertIn("config.yaml", names)
            self.assertEqual(
                yaml.safe_load(config_path.read_text(encoding="utf-8"))["network"]["GATEWAY_PORT"],
                5050,
            )

    def test_every_set_leaves_the_previous_file_recoverable(self):
        # The snapshot is taken by ConfigManager itself rather than by each caller, so
        # anything that writes a key -- the daemon rotating an Ergo node, a CLI command
        # storing a reputation proof id -- gets one without having to ask.
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")

            manager = self._manager(config_path)
            manager.set("network.GATEWAY_PORT", 5050)

            backups = [
                p for p in Path(tmpdir).iterdir()
                if re.fullmatch(r"config-\d{14}-\d{4}\.yaml", p.name)
            ]
            self.assertTrue(backups, "a set() left no backup beside the config")
            for snapshot in backups:
                self.assertEqual(
                    yaml.safe_load(snapshot.read_text(encoding="utf-8"))["network"]["GATEWAY_PORT"],
                    4040,
                    f"{snapshot.name} holds the new value, not the one it replaced",
                )
            self.assertEqual(
                yaml.safe_load(config_path.read_text(encoding="utf-8"))["network"]["GATEWAY_PORT"],
                5050,
            )

    def test_two_writes_inside_one_second_keep_two_snapshots(self):
        # The UTC stamp has one-second resolution, so the random tail is the only
        # thing separating a burst of writes. Without it the second copy lands on the
        # first one's name and the state before the first write is gone -- which is
        # the state a revert would want.
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"

            config_path.write_text("first\n", encoding="utf-8")
            first = backup_config_file(str(config_path))
            config_path.write_text("second\n", encoding="utf-8")
            second = backup_config_file(str(config_path))

            self.assertNotEqual(first, second)
            self.assertEqual(Path(first).read_text(encoding="utf-8"), "first\n")
            self.assertEqual(Path(second).read_text(encoding="utf-8"), "second\n")

    def test_a_burst_of_writes_still_prunes_to_the_retention(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            for i in range(CONFIG_BACKUP_RETENTION + 2):
                config_path.write_text(f"write-{i}\n", encoding="utf-8")
                backup_config_file(str(config_path))

            backups = [
                p.name for p in Path(tmpdir).iterdir()
                if re.fullmatch(r"config-\d{14}-\d{4}\.yaml", p.name)
            ]
            self.assertEqual(len(backups), CONFIG_BACKUP_RETENTION, backups)

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


if __name__ == "__main__":
    unittest.main()
