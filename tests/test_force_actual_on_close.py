"""`activity_window.FORCE_ACTUAL_ON_CLOSE_POLICY` (issue #344)."""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.utils import activity_window
    from src.utils.config_validation import ConfigValidationError, validate_host_policy_config
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ForceActualOnCloseTests(unittest.TestCase):
    def _read(self, value):
        values = {"activity_window.FORCE_ACTUAL_ON_CLOSE_POLICY": value}
        with mock.patch.object(activity_window.env_manager, "get",
                               side_effect=lambda key, default=None: values.get(key, default)):
            return activity_window.force_actual_on_close_policy()

    def setUp(self):
        activity_window._force_announced = False

    def test_it_is_off_by_default(self):
        with mock.patch.object(activity_window.env_manager, "get",
                               side_effect=lambda key, default=None: default):
            self.assertFalse(activity_window.force_actual_on_close_policy())

    def test_true_turns_it_on_and_a_line_is_logged_once(self):
        with mock.patch.object(activity_window, "_log") as log:
            self.assertTrue(self._read(True))
            self.assertTrue(self._read(True))
        self.assertEqual(log.call_count, 1)

    def test_a_value_that_is_not_true_reads_as_off(self):
        for value in ("true", 1, None, False):
            self.assertFalse(self._read(value), value)

    def test_the_validator_accepts_a_boolean(self):
        for value in (True, False):
            validate_host_policy_config(
                {"activity_window": {"FORCE_ACTUAL_ON_CLOSE_POLICY": value}})

    def test_the_validator_rejects_what_is_not_a_boolean(self):
        for value in ("yes", 1, "true", []):
            with self.assertRaises(ConfigValidationError):
                validate_host_policy_config(
                    {"activity_window": {"FORCE_ACTUAL_ON_CLOSE_POLICY": value}})


if __name__ == "__main__":
    unittest.main()
