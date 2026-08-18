import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SQLConnectionClientBalanceTests(unittest.TestCase):
    def test_get_client_balance_accepts_scientific_notation(self):
        conn = SQLConnection()

        with patch.object(
            conn,
            "_execute",
            return_value=_FakeCursor({"balance_mu": "1e+6", "last_usage": None}),
        ):
            gas_data = conn.get_client_balance("dev-external-1")

        self.assertIsNotNone(gas_data)
        self.assertEqual(gas_data[0], 10**6)


if __name__ == "__main__":
    unittest.main()
