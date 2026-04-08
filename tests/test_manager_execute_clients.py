import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.manager import manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    manager = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ManagerExecuteClientTests(unittest.TestCase):
    def test_get_execute_client_external_skips_clients_without_gas_row(self):
        client_gas = {
            "dev-external-broken": None,
            "dev-external-ok": (10**18, None, "1e+18"),
        }

        with patch.object(manager, "ensure_dev_client_pools"), patch.object(
            manager, "_get_external_dev_clients", return_value=["dev-external-broken", "dev-external-ok"]
        ), patch.object(
            manager.sc,
            "get_client_gas",
            side_effect=lambda client_id: client_gas.get(client_id),
        ):
            client_id = manager.get_execute_client(gas_amount=10**16, external=True)

        self.assertEqual(client_id, "dev-external-ok")


if __name__ == "__main__":
    unittest.main()
