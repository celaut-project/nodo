import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.manager import manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    manager = None  # type: ignore[assignment]


# The id shape the removed `execute --remote` pool used to mint. Nodes that ran it
# still have such rows in their clients table.
LEFTOVER_EXTERNAL_CLIENT = "dev-external-5f0c1d2e-0000-4000-8000-000000000000"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ManagerDevClientPoolTests(unittest.TestCase):
    def test_get_execute_client_draws_from_the_dev_pool(self):
        with patch.object(manager, "_acquire_dev_client", return_value="dev-1") as mock_acquire:
            client_id = manager.get_execute_client(amount_mu=10**16)

        self.assertEqual(client_id, "dev-1")
        mock_acquire.assert_called_once_with(
            manager.DEV_CLIENT_PREFIX,
            manager.STANDARD_DEV_CLIENT_POOL_SIZE,
            10**16,
        )

    def test_leftover_dev_external_client_is_still_a_dev_client(self):
        with patch.object(manager.sc, "client_exists", return_value=True):
            self.assertTrue(manager.is_dev_client_id(LEFTOVER_EXTERNAL_CLIENT))
            self.assertTrue(manager.descends_from_dev_client(LEFTOVER_EXTERNAL_CLIENT))

    def test_leftover_dev_external_client_is_reused_by_the_dev_pool(self):
        with patch.object(
            manager, "STANDARD_DEV_CLIENT_POOL_SIZE", 1
        ), patch.object(
            manager.sc, "get_dev_clients", return_value=[LEFTOVER_EXTERNAL_CLIENT]
        ), patch.object(
            manager.sc, "get_client_balance", return_value=(10**18, None, None)
        ), patch.object(
            manager.sc, "add_client"
        ) as add_client:
            manager.ensure_dev_client_pools()
            client_id = manager.get_execute_client(amount_mu=10**16)

        self.assertEqual(client_id, LEFTOVER_EXTERNAL_CLIENT)
        add_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
