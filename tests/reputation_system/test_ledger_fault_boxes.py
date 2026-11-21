"""The fault boxes on the ledger are an opt-in (#660, follow-up of #656)."""
import unittest
from unittest.mock import patch

from tests.reputation_system.test_offchain_reputation import IMPORT_ERROR

if IMPORT_ERROR is None:
    from src.reputation_system import offchain_proof


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LedgerFaultBoxesDefaultTests(unittest.TestCase):

    def test_a_node_puts_no_fault_box_on_the_ledger_by_default(self):
        with patch.object(offchain_proof, "_setting", side_effect=lambda key, default: default):
            self.assertEqual(offchain_proof.ledger_fault_reports(), [])

    def test_the_operator_can_turn_them_on(self):
        with patch.object(offchain_proof, "_setting", return_value=3), \
                patch("src.database.sql_connection.SQLConnection") as sql:
            sql.return_value.get_fault_evidence.return_value = []
            self.assertEqual(offchain_proof.ledger_fault_reports(), [])
            sql.return_value.get_fault_evidence.assert_called_once_with(3)

    def test_a_value_that_is_not_a_number_means_none(self):
        with patch.object(offchain_proof, "_setting", return_value="many"):
            self.assertEqual(offchain_proof.ledger_fault_reports(), [])
