"""A local instance can pay into its own balance (#630)."""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.gateway import client_gate
except Exception as import_exc:  # pragma: no cover
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DepositAccountTests(unittest.TestCase):

    def _sc(self, clients=(), instances=()):
        sc = mock.MagicMock()
        sc.client_exists.side_effect = lambda client_id: client_id in clients
        sc.internal_instance_exists.side_effect = lambda id: id in instances
        return sc

    def test_client_keeps_its_own_account(self):
        with mock.patch.object(client_gate, "sc", self._sc(clients=("c1",))):
            self.assertEqual(client_gate.deposit_account("c1", "c1"), "c1")

    def test_instance_pays_itself_with_empty_id(self):
        with mock.patch.object(client_gate, "sc", self._sc(instances=("i1",))):
            self.assertEqual(client_gate.deposit_account("i1", ""), "i1")

    def test_instance_can_name_itself(self):
        with mock.patch.object(client_gate, "sc", self._sc(instances=("i1",))):
            self.assertEqual(client_gate.deposit_account("i1", "i1"), "i1")

    def test_instance_cannot_name_an_unknown_id(self):
        with mock.patch.object(client_gate, "sc", self._sc(instances=("i1",))):
            with self.assertRaises(client_gate.ClientRequired):
                client_gate.deposit_account("i1", "made-up")

    def test_instance_cannot_name_another_instance(self):
        with mock.patch.object(client_gate, "sc", self._sc(instances=("i1", "i2"))):
            with self.assertRaises(client_gate.ClientRequired):
                client_gate.deposit_account("i1", "i2")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PayableAccountTests(unittest.TestCase):
    """A payment for an instance that stopped goes to its father (#630)."""

    def _sql(self, live=(), fathers=None):
        from src.database.sql_connection import SQLConnection
        sql = mock.MagicMock()
        sql.account_exists.side_effect = lambda account_id: account_id in live
        sql.father_of.side_effect = lambda account_id, with_dead=False: (fathers or {}).get(account_id)
        return lambda account_id: SQLConnection.payable_account(sql, account_id)

    def test_a_live_account_is_credited_itself(self):
        self.assertEqual(self._sql(live=("i1",))("i1"), "i1")

    def test_a_stopped_instance_credits_its_father(self):
        self.assertEqual(self._sql(live=("c1",), fathers={"i1": "c1"})("i1"), "c1")

    def test_a_stopped_father_passes_it_up(self):
        self.assertEqual(self._sql(live=("c1",), fathers={"i2": "i1", "i1": "c1"})("i2"), "c1")

    def test_no_live_ancestor_gives_none(self):
        self.assertIsNone(self._sql(fathers={"i1": "gone"})("i1"))

    def test_the_manager_credits_the_father_and_names_the_instance(self):
        from src.manager import manager
        from src.database import ledger
        sc = mock.MagicMock()
        sc.payable_account.return_value = "c1"
        sc.client_exists.return_value = False  # i1 is an instance: it is the payer
        with mock.patch.object(manager, "sc", sc):
            manager.increase_local_balance_for_client("i1", 500, detail={"ledger": "ergo"})
        sc.move_balance.assert_called_once_with(
            "c1", 500, ledger.DEPOSIT, instance_token="i1",
            detail={"ledger": "ergo", "reason": "instance_stopped", "actor": "instance"})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TxHistoryAttributionTests(unittest.TestCase):

    def test_deposit_tokens_map_to_their_account(self):
        from src.commands import tx_history
        sql = mock.MagicMock()
        sql.get_deposit_tokens.return_value = [{"id": "t", "account_id": "i1", "status": "payed"}]
        with mock.patch("src.database.sql_connection.SQLConnection", return_value=sql):
            self.assertEqual(tx_history._clients_by_deposit_token(), {"t": "i1"})


if __name__ == "__main__":
    unittest.main()
