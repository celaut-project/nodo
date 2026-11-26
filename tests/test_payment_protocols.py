"""A payment contract is described by a protocol whose formal is its identity (#671)."""
import os
import unittest
from hashlib import sha3_256

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.utils import descriptor_file, ledger_descriptors
    from src.utils.contract_xattrs import set_script, set_token_id
    from src.utils.payment_protocol import protocol_hash, protocol_id
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

DOCS = os.path.join(os.path.dirname(__file__), "..", "contract", "descriptors", "contracts", "payment")


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class PaymentProtocolTests(unittest.TestCase):
    def test_the_formal_bytes_are_the_ones_published_in_docs(self):
        for name, protocol in (
            ("ergo-p2pk", ledger_descriptors.ergo_payment_protocol()),
            ("bitcoin-p2wpkh", ledger_descriptors.bitcoin_payment_protocol()),
        ):
            path = os.path.join(DOCS, f"{name}.descriptor")
            self.assertEqual(descriptor_file.read(path).formal, protocol_id(protocol), name)

    def test_the_hash_is_the_hash_of_the_formal(self):
        protocol = ledger_descriptors.ergo_payment_protocol()
        self.assertEqual(protocol_hash(protocol), sha3_256(protocol_id(protocol)).hexdigest())
        self.assertIsNone(protocol_hash(celaut_pb2.Contract.Protocol(tags=["ergo-p2pk"])))

    def test_the_contract_modules_use_the_hash_of_their_protocol(self):
        from src.payment_system.contracts.bitcoin import interface as bitcoin
        from src.payment_system.contracts.ergo import interface as ergo

        self.assertEqual(ergo.CONTRACT_HASH, protocol_hash(ledger_descriptors.ergo_payment_protocol()))
        self.assertEqual(bitcoin.CONTRACT_HASH, protocol_hash(ledger_descriptors.bitcoin_payment_protocol()))
        self.assertNotEqual(ergo.CONTRACT_HASH, bitcoin.CONTRACT_HASH)

    def test_the_payee_parameter_does_not_change_the_identity(self):
        protocol = ledger_descriptors.bitcoin_payment_protocol()
        other = celaut_pb2.Contract.Protocol()
        other.CopyFrom(protocol)
        other.formal = bytes(protocol.formal).replace(b"payment.min_confirmations=1", b"payment.min_confirmations=5")
        self.assertEqual(protocol_id(protocol), protocol_id(other))

    def test_a_contract_has_no_contract_type_xattr(self):
        contract = celaut_pb2.Contract(ledger=ledger_descriptors.ergo_payment_ledger(),
                                       protocol=ledger_descriptors.ergo_payment_protocol())
        set_token_id(contract, "ERG")
        set_script(contract, b"\x00")
        self.assertEqual([kv.key for kv in contract.xattrs], ["script", "token_id"])

    def test_the_node_does_not_store_a_contract_without_a_protocol(self):
        import sqlite3

        from src.database import migrate
        from src.database.sql_connection import SQLConnection

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        migrate.ensure_tables(connection.cursor(), ("contract_instance", "ledger"))
        sql = SQLConnection()
        saved = SQLConnection._connection
        SQLConnection._connection = connection
        self.addCleanup(lambda: setattr(SQLConnection, "_connection", saved))

        contract = celaut_pb2.Contract(ledger=ledger_descriptors.ergo_payment_ledger())
        set_token_id(contract, "ERG")
        set_script(contract, b"\x00\x01")
        sql.add_contract(contract=contract, peer_id="peer-1", mu_per_unit=1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM contract_instance").fetchone()[0], 0)

        contract.protocol.CopyFrom(ledger_descriptors.ergo_payment_protocol())
        sql.add_contract(contract=contract, peer_id="peer-1", mu_per_unit=1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM contract_instance").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
