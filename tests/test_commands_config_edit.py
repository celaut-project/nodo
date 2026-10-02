"""``nodo config``: the TUI's configuration transaction, without a terminal.

Covers the pure parts (paths, masking, profile matching), that the profile
catalogue is the one ``nodo tui`` ships (parsed out of cell.rs), and the
transaction itself against a real ``yq`` on a scratch config.yaml: written in
place with comments kept, refused without root on a serving node, and rolled
back when the restart does not bring the node back.
"""
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import yaml

from tests.config_bootstrap import load_example_config

load_example_config()

from src.commands import config_edit as ce  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CELL_RS = os.path.join(_ROOT, "src", "commands", "tui", "src", "cell.rs")

SCRATCH = """# a comment that must survive
network:
  GATEWAY_PORT: 0   # inline comment
  DELEGATE_EXECUTION: false
service_networks:
  blacklist: []
wallet:
  mnemonic: "abandon abandon"
"""


def _rust_profiles():
    """PROFILES out of cell.rs: [(id, [(path, value), ...]), ...]."""
    with open(CELL_RS) as f:
        source = f.read()
    block = source[source.index("static PROFILES"):source.index("pub fn profiles()")]
    profiles = []
    for chunk in block.split("Profile {")[1:]:
        profile_id = re.search(r'id:\s*"([^"]+)"', chunk).group(1)
        writes = re.findall(r'\(\s*"([^"]+)",\s*"([^"]*)"\s*\)', chunk)
        profiles.append((profile_id, writes))
    return profiles


class PathTests(unittest.TestCase):
    def test_parse_and_format_round_trip(self):
        for text, segments in (("network.GATEWAY_PORT", ["network", "GATEWAY_PORT"]),
                               ("core_services[1].id", ["core_services", 1, "id"]),
                               ("a[0][2]", ["a", 0, 2])):
            self.assertEqual(ce.parse_path(text), segments)
            self.assertEqual(ce.format_path(segments), text)

    def test_malformed_paths_are_refused(self):
        for text in ("", ".a", "a..b", "a.", "a[x]"):
            with self.assertRaises(ValueError, msg=text):
                ce.parse_path(text)

    def test_yq_expression_quotes_keys(self):
        self.assertEqual(ce.yq_path_expression(["a", 1, 'we"ird']), '.["a"][1]["we\\"ird"]')

    def test_secret_paths_match_the_tui(self):
        for path in ("ledgers.ergo.wallet.mnemonic", "x.api_key", "a.token", "a.refresh_token",
                     "list[0].password"):
            self.assertTrue(ce.is_secret_path(path), path)
        for path in ("network.GATEWAY_PORT", "token_id", "tokens[0]"):
            self.assertFalse(ce.is_secret_path(path), path)

    def test_values_keep_their_yaml_type(self):
        self.assertIs(ce.parse_value("true"), True)
        self.assertEqual(ce.parse_value("[]"), [])
        self.assertEqual(ce.parse_value("1.5"), 1.5)
        self.assertEqual(ce.parse_value('"5"'), "5")

    def test_comparison_is_typed_like_serde_yaml(self):
        self.assertFalse(ce._same(1, 1.0))
        self.assertFalse(ce._same(True, 1))
        self.assertTrue(ce._same([1, "a"], [1, "a"]))


class ProfileCatalogueTests(unittest.TestCase):
    def test_same_catalogue_as_the_tui(self):
        rust = _rust_profiles()
        self.assertTrue(rust, "could not parse PROFILES out of cell.rs")
        python = [(p["id"], list(p["writes"])) for p in ce.PROFILES]
        self.assertEqual(python, rust)

    def test_every_profile_key_exists_in_the_example_config(self):
        with open(os.path.join(_ROOT, "config.example.yaml")) as f:
            example = yaml.safe_load(f)
        for profile in ce.PROFILES:
            for path, _ in profile["writes"]:
                found, _ = ce.value_at(example, ce.parse_path(path))
                self.assertTrue(found, f"{profile['id']}: {path}")

    def test_a_document_in_a_posture_matches_it_exactly(self):
        document = {}
        for path, value in ce.PROFILES[2]["writes"]:
            *parents, leaf = ce.parse_path(path)
            node = document
            for key in parents:
                node = node.setdefault(key, {})
            node[leaf] = yaml.safe_load(value)
        report = ce.closest_profile(document)
        self.assertEqual(report["id"], ce.PROFILES[2]["id"])
        self.assertEqual(report["deviations"], [])


@unittest.skipUnless(shutil.which("yq"), "yq is not installed")
class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "config.yaml")
        with open(self.path, "w") as f:
            f.write(SCRATCH)
        patches = [
            patch.object(ce, "config_path", return_value=self.path),
            patch.object(ce, "_yq_binary", return_value=shutil.which("yq")),
            patch.object(ce, "_systemd_state", return_value="absent"),
            patch.object(ce, "_forget_gateway_verdicts"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.directory.cleanup)

    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            ok = ce.config_command(argv)
        return ok, out.getvalue()

    def _backups(self):
        return [n for n in os.listdir(self.directory.name) if n.startswith("config-")]

    def test_set_writes_in_place_typed_and_keeps_comments(self):
        ok, out = self._run(["set", "network.DELEGATE_EXECUTION=true",
                             "service_networks.blacklist=[\"*.evil\"]", "--json"])
        self.assertTrue(ok, out)
        self.assertEqual(json.loads(out)["outcome"], "not-running")
        with open(self.path) as f:
            text = f.read()
        self.assertIn("# a comment that must survive", text)
        document = yaml.safe_load(text)
        self.assertIs(document["network"]["DELEGATE_EXECUTION"], True)
        self.assertEqual(document["service_networks"]["blacklist"], ["*.evil"])
        self.assertEqual(len(self._backups()), 1)

    def test_get_masks_secrets_unless_asked(self):
        ok, out = self._run(["get", "wallet", "--json"])
        self.assertTrue(ok)
        self.assertEqual(json.loads(out)["value"], {"mnemonic": ce.MASK})
        ok, out = self._run(["get", "wallet.mnemonic", "--json", "--show-secrets"])
        self.assertEqual(json.loads(out)["value"], "abandon abandon")

    def test_get_unknown_key_fails(self):
        ok, _ = self._run(["get", "network.NOPE"])
        self.assertFalse(ok)

    def test_append_and_remove_list_elements(self):
        self.assertTrue(self._run(["append", "service_networks.blacklist", "dns:*"])[0])
        self.assertTrue(self._run(["append", "service_networks.blacklist", "10.0.0.0/8"])[0])
        self.assertTrue(self._run(["remove", "service_networks.blacklist[0]"])[0])
        with open(self.path) as f:
            self.assertEqual(yaml.safe_load(f)["service_networks"]["blacklist"], ["10.0.0.0/8"])

    def test_remove_refuses_a_key(self):
        ok, _ = self._run(["remove", "network.DELEGATE_EXECUTION"])
        self.assertFalse(ok)

    def test_a_serving_node_needs_root(self):
        with patch.object(ce, "_serving_on", return_value=True), \
                patch.object(ce.os, "geteuid", return_value=1000):
            ok, out = self._run(["set", "network.DELEGATE_EXECUTION=true", "--json"])
        self.assertFalse(ok)
        self.assertIn("root", json.loads(out)["error"])
        with open(self.path) as f:
            self.assertEqual(f.read(), SCRATCH)
        self.assertEqual(self._backups(), [])

    def test_a_restart_that_does_not_come_back_is_rolled_back(self):
        with patch.object(ce, "_serving_on", return_value=True), \
                patch.object(ce.os, "geteuid", return_value=0), \
                patch.object(ce, "_restart", return_value=(True, "")) as restart, \
                patch.object(ce, "_wait_until_serving", return_value=False):
            ok, out = self._run(["set", "network.DELEGATE_EXECUTION=true", "--json"])
        self.assertFalse(ok)
        self.assertEqual(json.loads(out)["outcome"], "reverted")
        with open(self.path) as f:
            self.assertEqual(f.read(), SCRATCH)
        self.assertEqual(restart.call_count, 2)  # onto the change, then back off it

    def test_a_successful_restart_keeps_the_change(self):
        with patch.object(ce, "_serving_on", return_value=True), \
                patch.object(ce.os, "geteuid", return_value=0), \
                patch.object(ce, "_restart", return_value=(True, "")), \
                patch.object(ce, "_wait_until_serving", return_value=True):
            ok, out = self._run(["set", "network.DELEGATE_EXECUTION=true", "--json"])
        self.assertTrue(ok)
        self.assertEqual(json.loads(out)["outcome"], "restarted")

    def test_profile_apply_writes_only_the_deviations(self):
        ok, out = self._run(["profile", "just-me", "--json"])
        self.assertTrue(ok)
        deviations = json.loads(out)["profile"]["deviations"]
        self.assertNotIn("network.DELEGATE_EXECUTION", [d["path"] for d in deviations])
        ok, out = self._run(["profile", "just-me", "--apply", "--json"])
        self.assertTrue(ok, out)
        ok, out = self._run(["profile", "--json"])
        report = json.loads(out)
        self.assertEqual(report["closest"], "just-me")
        self.assertEqual(report["profiles"][0]["deviations"], [])

    def test_malformed_value_writes_nothing(self):
        ok, _ = self._run(["set", "network.DELEGATE_EXECUTION=[unclosed"])
        self.assertFalse(ok)
        self.assertEqual(self._backups(), [])


if __name__ == "__main__":
    unittest.main()
