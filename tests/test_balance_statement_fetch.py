"""Reading a statement from a peer (#570): ``fetch`` refuses what the peer did not sign,
and what is not the account and the range that was asked for."""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from mnemonic import Mnemonic

    from src.database import ledger
    from src.identity import node_identity
    from src.manager import balance_statement
    from tests.statement_chain import FakeLedger
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FetchTests(unittest.TestCase):

    def setUp(self):
        mnemonic = Mnemonic("english").generate()
        patcher = patch.object(node_identity, "get_identity_mnemonic", return_value=mnemonic)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.peer_id = node_identity.get_node_public_key_hex()
        self.book = FakeLedger("client-on-peer")
        self.book.add(100, ledger.CREDIT)
        self.book.add(-10, ledger.TOP_UP)

    def signed(self, from_seq=1, to_seq=0):
        statement = self.book.statement(from_seq=from_seq, to_seq=to_seq)
        statement.signature = node_identity.sign_peer_payload(
            node_identity.statement_payload(self.peer_id, balance_statement.statement_digest(statement))
        )
        return statement

    def fetch(self, answer, **kwargs):
        with patch("src.identity.grpc_transport.peer_channel"), \
                patch("src.utils.bee_client.BeeClient.get_balance_statement",
                      return_value=answer) as call:
            result = balance_statement.fetch(self.peer_id, "client-on-peer", **kwargs)
        return result, call

    def test_a_statement_that_the_peer_signed_is_accepted(self):
        statement = self.signed(from_seq=2)

        result, call = self.fetch(statement, from_seq=2)

        self.assertEqual(result, statement)
        self.assertEqual((call.call_args.kwargs["from_seq"], call.call_args.kwargs["to_seq"]), (2, 0))

    def test_no_answer_is_refused(self):
        with self.assertRaises(balance_statement.StatementInvalid):
            self.fetch(None, from_seq=1)

    def test_another_account_is_refused(self):
        statement = self.signed()
        statement.account_ref = ledger.account_ref("someone-else")

        with self.assertRaises(balance_statement.StatementInvalid):
            self.fetch(statement, from_seq=1)

    def test_another_range_is_refused(self):
        with self.assertRaises(balance_statement.StatementInvalid):
            self.fetch(self.signed(from_seq=1), from_seq=2)
        with self.assertRaises(balance_statement.StatementInvalid):
            self.fetch(self.signed(from_seq=1, to_seq=1), from_seq=1)

    def test_a_statement_that_another_key_signed_is_refused(self):
        statement = self.signed()
        with patch.object(node_identity, "get_identity_mnemonic",
                          return_value=Mnemonic("english").generate()):
            other = node_identity.get_node_public_key_hex()
        with patch("src.identity.grpc_transport.peer_channel"), \
                patch("src.utils.bee_client.BeeClient.get_balance_statement", return_value=statement):
            with self.assertRaises(balance_statement.StatementInvalid):
                balance_statement.fetch(other, "client-on-peer", from_seq=1)

    def test_a_statement_with_a_changed_movement_is_refused(self):
        statement = self.signed()
        statement.movements[1].amount.n = "-1"

        with self.assertRaises(balance_statement.StatementInvalid):
            self.fetch(statement, from_seq=1)

    def test_an_unsigned_statement_is_refused(self):
        statement = self.book.statement(from_seq=1)

        with self.assertRaises(balance_statement.StatementInvalid):
            self.fetch(statement, from_seq=1)


if __name__ == "__main__":
    unittest.main()
