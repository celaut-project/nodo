import tempfile
import unittest
from pathlib import Path

import yaml

from src.utils.config import ConfigManager, to_yaml_safe
from src.utils.singleton import Singleton


class _JStringLike(str):
    """Stand-in for jpype's java.lang.String: a str subclass PyYAML rejects."""


class _ForeignObject:
    def __init__(self, value):
        self._jstr = value

    def __str__(self):
        return self._jstr


# The exact shape a jpype java.lang.String serialized to before this fix.
POISONED_CONFIG = (
    "reputation:\n"
    "  REPUTATION_PROOF_ID: !!python/object:jpype._jstring.java.lang.String\n"
    "    _jstr: f84f940a811c4d3116651eafc925f4f04d9e686ab3fcac2c29cac97bd90fe982\n"
    "network:\n"
    "  GATEWAY_PORT: 4040\n"
)
PROOF_ID = "f84f940a811c4d3116651eafc925f4f04d9e686ab3fcac2c29cac97bd90fe982"


class ToYamlSafeTests(unittest.TestCase):
    def test_str_subclass_is_normalized_to_exact_str(self):
        value = _JStringLike("abc")
        result = to_yaml_safe(value)
        self.assertIs(type(result), str)
        self.assertEqual(result, "abc")

    def test_foreign_object_becomes_its_string(self):
        self.assertEqual(to_yaml_safe(_ForeignObject("xyz")), "xyz")

    def test_containers_are_recursed(self):
        data = {"a": _JStringLike("1"), "b": [_JStringLike("2"), 3], "c": ("t",)}
        result = to_yaml_safe(data)
        self.assertIs(type(result["a"]), str)
        self.assertIs(type(result["b"][0]), str)
        self.assertEqual(result["b"][1], 3)
        self.assertEqual(result["c"], ["t"])

    def test_native_values_pass_through(self):
        for value in (None, True, 5, 1.5, "plain"):
            self.assertEqual(to_yaml_safe(value), value)

    def test_safe_dump_after_coercion_has_no_python_object_tag(self):
        dumped = yaml.safe_dump(to_yaml_safe({"id": _JStringLike(PROOF_ID)}))
        self.assertNotIn("python/object", dumped)
        # Round-trips through the safe loader that nodo uses.
        self.assertEqual(yaml.safe_load(dumped), {"id": PROOF_ID})


class ConfigRecoveryTests(unittest.TestCase):
    def setUp(self):
        Singleton._instances.pop(ConfigManager, None)

    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def test_poisoned_config_fails_plain_safe_load(self):
        # Establishes that this file really is the failure case users hit.
        with self.assertRaises(yaml.YAMLError):
            yaml.safe_load(POISONED_CONFIG)

    def test_manager_recovers_and_rewrites_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(POISONED_CONFIG, encoding="utf-8")

            manager = ConfigManager(config_path=str(config_path))
            manager.load_config()

            # Value recovered as a plain string.
            self.assertEqual(manager.get("reputation.REPUTATION_PROOF_ID"), PROOF_ID)

            # File was rewritten cleanly: no foreign tag, and it now safe_loads.
            rewritten = config_path.read_text(encoding="utf-8")
            self.assertNotIn("python/object", rewritten)
            reloaded = yaml.safe_load(rewritten)
            self.assertEqual(reloaded["reputation"]["REPUTATION_PROOF_ID"], PROOF_ID)

    def test_set_foreign_value_writes_clean_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("reputation:\n  REPUTATION_PROOF_ID: ''\n", encoding="utf-8")

            manager = ConfigManager(config_path=str(config_path))
            manager.load_config()
            # Simulate transaction.py storing a Java string.
            manager.set("reputation.REPUTATION_PROOF_ID", _JStringLike(PROOF_ID))

            self.assertIs(type(manager.get("reputation.REPUTATION_PROOF_ID")), str)
            rewritten = config_path.read_text(encoding="utf-8")
            self.assertNotIn("python/object", rewritten)
            self.assertEqual(
                yaml.safe_load(rewritten)["reputation"]["REPUTATION_PROOF_ID"], PROOF_ID
            )


if __name__ == "__main__":
    unittest.main()
