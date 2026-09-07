"""A guest's serial log belongs to that guest, and always exists (issue #322).

The serial log is evidence rather than convenience output: `guest_panic.guest_panic_line`
reads it to decide whether a guest panicked, and the maintenance tick kills that VM and
penalises its instance -100 INSTANCE_LOST. So the two things an operator could once
choose were the two ways of breaking it -- a configured path was one file for every VM,
making one guest's panic read as every guest's, and turning the stream off left the check
with nothing to read and quietly retired the reaping. Both are now fixed in code, and
these pin that.
"""
import unittest
from pathlib import Path

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.utils.config import ConfigManager
    from src.virtualizers.ch import execute as ch
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GuestSerialLogIsPerVMTests(unittest.TestCase):
    A = Path("/run/nodo/vm-a")
    B = Path("/run/nodo/vm-b")

    def test_the_log_sits_in_the_vms_own_runtime_dir(self):
        args, serial_log = ch._resolve_ch_stream_args(runtime_dir=self.A)
        self.assertEqual(serial_log, self.A / ch.SERIAL_LOG_NAME)
        self.assertIn(f"file={serial_log}", args)

    def test_two_vms_never_share_a_log(self):
        _, first = ch._resolve_ch_stream_args(runtime_dir=self.A)
        _, second = ch._resolve_ch_stream_args(runtime_dir=self.B)
        self.assertNotEqual(first, second)

    def test_a_log_is_always_recorded(self):
        # `guest_panic_line` treats a missing `serial_log` as "cannot tell" and leaves
        # the guest alone, so a path that could come back None would be a panic check
        # that could be switched off.
        self.assertIsNotNone(ch._resolve_ch_stream_args(runtime_dir=self.A)[1])

    def test_the_virtio_console_stays_off(self):
        # A second copy of what --serial already captures, since the cmdline points the
        # kernel at ttyS0.
        args, _ = ch._resolve_ch_stream_args(runtime_dir=self.A)
        self.assertEqual(args[args.index("--console") + 1], "off")

    def test_the_streams_are_not_configurable(self):
        # The keys are gone, not merely defaulted: config could not reintroduce either
        # failure even if an old config.yaml still carried them.
        config = ConfigManager()
        self.assertIsNone(config.get("virtualizers.ch.SERIAL_MODE"))
        self.assertIsNone(config.get("virtualizers.ch.CONSOLE_MODE"))
        self.assertFalse(hasattr(ch, "CH_SERIAL_MODE"))
        self.assertFalse(hasattr(ch, "CH_CONSOLE_MODE"))


if __name__ == "__main__":
    unittest.main()
