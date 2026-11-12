"""An instance that pays for its own balance did not consume that MU (#630)."""
import unittest

IMPORT_ERROR = None
try:
    from src.manager import statement_check as check
    from src.database import ledger
except Exception as import_exc:  # pragma: no cover
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OwnPaymentIsNotConsumptionTests(unittest.TestCase):

    def test_a_deposit_on_a_delegated_child_is_not_subtracted_from_its_consumption(self):
        Row = check.Row
        rows = [Row(2, 0, -100, 0, ledger.MAINTENANCE, ""), Row(3, 0, 500, 0, ledger.DEPOSIT, ""),
                Row(4, 0, 300, 0, ledger.TOP_UP, "")]
        self.assertEqual(check.consumption(rows), 100)


if __name__ == "__main__":
    unittest.main()
