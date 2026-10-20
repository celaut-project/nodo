"""The example config must not tell operators that an out of balance stop costs reputation."""
import pathlib
import unittest

_EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "config.example.yaml"


class ConfigExampleOutOfBalanceTest(unittest.TestCase):
    def test_example_does_not_name_the_removed_penalty(self):
        # Issue #562 removed the penalty of the service for this stop. The comment of
        # MAX_PRICE_VOLATILITY must not say that the penalty exists.
        self.assertNotIn("INSTANCE_OUT_OF_BALANCE", _EXAMPLE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
