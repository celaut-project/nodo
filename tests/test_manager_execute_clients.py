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
    def test_get_execute_client_uses_shared_acquire_path_for_standard_clients(self):
        with patch.object(manager, "_acquire_dev_client", return_value="dev-1") as mock_acquire:
            client_id = manager.get_execute_client(amount_mu=10**16, external=False)

        self.assertEqual(client_id, "dev-1")
        mock_acquire.assert_called_once_with(
            manager.DEV_CLIENT_PREFIX,
            manager.STANDARD_DEV_CLIENT_POOL_SIZE,
            10**16,
        )

    def test_get_execute_client_uses_shared_acquire_path_for_external_clients(self):
        with patch.object(manager, "_acquire_dev_client", return_value="dev-external-1") as mock_acquire:
            client_id = manager.get_execute_client(amount_mu=10**16, external=True)

        self.assertEqual(client_id, "dev-external-1")
        mock_acquire.assert_called_once_with(
            manager.EXTERNAL_DEV_CLIENT_PREFIX,
            manager.DEV_EXTERNAL_CLIENT_POOL_SIZE,
            10**16,
        )


if __name__ == "__main__":
    unittest.main()
