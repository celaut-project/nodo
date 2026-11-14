"""A ledger tag names the chain and the network: ``bitcoin-regtest``, ``ergo-mainnet``.

One interface module (Bitcoin, Ergo) settles on several networks, and each network is a
different ledger. Before, the tag named only the chain ("bitcoin"), and a
``contract_instance`` row did not say on which network it was written. A node that
changed ``ledgers.bitcoin.NETWORK`` kept its old row and announced the old script under
the new network's declaration: a payer could pay a script the node no longer watched.

Now the tag names the network, each row keeps the ``formal`` it was announced with, and
a reader asks only for the rows of the ledger it means.

Only SQLite in memory and a mocked config here. Nothing is signed or sent.
"""
import sqlite3
import unittest
from hashlib import sha3_256
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
    from src.payment_system.contracts.bitcoin import interface as btc
    from src.payment_system.contracts.registry import PaymentMethod
    from src.utils import ledger_descriptors
    from src.utils.contract_xattrs import set_contract_type, set_script, set_token_id
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

MAINNET_GENESIS = "b0244dfc267baca974a4caee06120321562784303a8a688976ae56170e4d175b"
OTHER_GENESIS = "0123456789abcdef" + "00" * 24
P2WPKH_HASH = sha3_256(b"p2wpkh").hexdigest()


def _configured(**values):
    """``ledger_descriptors`` reading ``values`` on top of the config of the tests."""
    real = ledger_descriptors.ConfigManager

    class _Config:
        def get(self, key, default=None):
            if key in values:
                return values[key]
            return real().get(key, default)

    return mock.patch.object(ledger_descriptors, "ConfigManager", _Config)


def _on(network):
    return _configured(**{"ledgers.bitcoin.NETWORK": network})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TagTests(unittest.TestCase):

    def test_the_bitcoin_tag_names_the_network(self):
        for network in ("mainnet", "testnet", "signet", "regtest"):
            with self.subTest(network=network), _on(network):
                self.assertEqual(ledger_descriptors.bitcoin_tag(), f"bitcoin-{network}")
                ledger = ledger_descriptors.bitcoin_payment_ledger()
                self.assertEqual(list(ledger.tags), [f"bitcoin-{network}"])

    def test_the_ergo_tag_names_the_network(self):
        with _configured(**{"ledgers.ergo.GENESIS_BLOCK_ID": MAINNET_GENESIS.upper()}):
            self.assertEqual(ledger_descriptors.ergo_tag(), "ergo-mainnet")
            self.assertEqual(list(ledger_descriptors.ergo_payment_ledger().tags),
                             ["ergo-mainnet"])
            self.assertEqual(list(ledger_descriptors.ergo_reputation_ledger().tags),
                             ["ergo-mainnet"])
        with _configured(**{"ledgers.ergo.GENESIS_BLOCK_ID": OTHER_GENESIS}):
            self.assertEqual(ledger_descriptors.ergo_tag(), "ergo-0123456789abcdef")
        with _configured(**{"ledgers.ergo.GENESIS_BLOCK_ID": ""}):
            self.assertEqual(ledger_descriptors.ergo_tag(), "ergo-unset")

    def test_the_chain_of_a_tag_is_the_config_block(self):
        self.assertEqual(ledger_descriptors.chain_of("bitcoin-regtest"), "bitcoin")
        self.assertEqual(ledger_descriptors.chain_of("ergo-0123456789abcdef"), "ergo")
        self.assertEqual(ledger_descriptors.chain_of("bitcoin"), "bitcoin")
        self.assertEqual(ledger_descriptors.chain_of("simulated"), "simulated")
        with _on("regtest"):
            self.assertEqual(ledger_descriptors.ledger_tag("bitcoin"), "bitcoin-regtest")
            self.assertEqual(ledger_descriptors.ledger_tag("bitcoin-mainnet"),
                             "bitcoin-regtest")
        self.assertEqual(ledger_descriptors.ledger_tag("simulated"), "simulated")

    def test_only_the_network_this_node_is_on_has_a_declaration(self):
        with _on("regtest"):
            self.assertIsNotNone(ledger_descriptors.payment_ledger("bitcoin-regtest"))
            self.assertIsNone(ledger_descriptors.payment_ledger("bitcoin-mainnet"))
            # The chain alone is the tag of a node from before this change.
            self.assertIsNone(ledger_descriptors.payment_ledger("bitcoin"))
            self.assertIsNone(ledger_descriptors.payment_ledger("dogecoin-mainnet"))

    def test_two_networks_are_two_ledgers(self):
        with _on("mainnet"):
            mainnet = ledger_descriptors.bitcoin_payment_ledger()
        with _on("regtest"):
            regtest = ledger_descriptors.bitcoin_payment_ledger()
        self.assertNotEqual(list(mainnet.tags), list(regtest.tags))
        self.assertFalse(ledger_descriptors.same_payment_ledger(mainnet, regtest))

    def test_a_method_is_keyed_by_the_network_and_configured_by_the_chain(self):
        method = PaymentMethod(btc, "BTC")
        with _on("regtest"):
            self.assertEqual(method.key.ledger, "bitcoin-regtest")
            self.assertEqual(btc.ledger_tag(), "bitcoin-regtest")
        # The config block and the command line keep the chain.
        self.assertEqual(btc.LEDGER, "bitcoin")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StoredRowTests(unittest.TestCase):

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        migrate.ensure_tables(self.connection.cursor(), ("contract_instance", "ledger"))
        self.sql = SQLConnection()
        saved = SQLConnection._connection
        SQLConnection._connection = self.connection
        self.addCleanup(lambda: setattr(SQLConnection, "_connection", saved))

    def _store(self, network, script, peer_id="LOCAL"):
        with _on(network):
            contract = celaut_pb2.Contract(ledger=ledger_descriptors.bitcoin_payment_ledger())
        set_token_id(contract, "BTC")
        set_script(contract, script)
        set_contract_type(contract, b"p2wpkh")
        self.sql.add_contract(contract=contract, peer_id=peer_id)
        return contract

    def _scripts(self, network, peer_id="LOCAL"):
        with _on(network):
            declaration = ledger_descriptors.bitcoin_payment_ledger()
        return [script for script, _tag, _asset in self.sql.get_peer_contract_instances(
            P2WPKH_HASH, peer_id, "BTC", ledger=declaration
        )]

    def test_the_row_keeps_the_formal_it_was_announced_with(self):
        contract = self._store("regtest", b"\x00\x14" + b"\x11" * 20)
        row = self.connection.execute(
            "SELECT ledger, formal FROM contract_instance"
        ).fetchone()
        self.assertEqual(row["ledger"], "bitcoin-regtest")
        self.assertEqual(bytes(row["formal"]), bytes(contract.ledger.formal))

    def test_a_row_of_another_network_is_not_read(self):
        # The node was on mainnet, then the config changed NETWORK to regtest.
        old, new = b"\x00\x14" + b"\x11" * 20, b"\x00\x14" + b"\x22" * 20
        self._store("mainnet", old)
        self._store("regtest", new)
        self.assertEqual(self._scripts("regtest"), [new])
        self.assertEqual(self._scripts("mainnet"), [old])
        # Without a ledger, every row: the old behaviour, for a caller that asks so.
        self.assertEqual(
            len(list(self.sql.get_peer_contract_instances(P2WPKH_HASH, "LOCAL", "BTC"))), 2
        )

    def test_a_row_with_the_tag_but_another_declaration_is_not_read(self):
        self._store("regtest", b"\x00\x14" + b"\x11" * 20)
        self.connection.execute(
            "UPDATE contract_instance SET formal = CAST(REPLACE(formal, "
            "CAST('satoshi' AS BLOB), CAST('millisatoshi' AS BLOB)) AS BLOB)"
        )
        self.assertEqual(self._scripts("regtest"), [])

    def test_the_payee_parameters_do_not_hide_a_peer_row(self):
        # A peer that waits for another number of confirmations is on the same ledger.
        with _on("regtest"), _configured(**{
            "ledgers.bitcoin.NETWORK": "regtest",
            "ledgers.bitcoin.payments.MIN_CONFIRMATIONS": 6,
        }):
            contract = celaut_pb2.Contract(ledger=ledger_descriptors.bitcoin_payment_ledger())
        set_token_id(contract, "BTC")
        set_script(contract, b"\x00\x14" + b"\x33" * 20)
        set_contract_type(contract, b"p2wpkh")
        self.sql.add_contract(contract=contract, peer_id="peer-1")
        with _on("regtest"), _configured(**{
            "ledgers.bitcoin.NETWORK": "regtest",
            "ledgers.bitcoin.payments.MIN_CONFIRMATIONS": 1,
        }):
            declaration = ledger_descriptors.bitcoin_payment_ledger()
        self.assertEqual(len(list(self.sql.get_peer_contract_instances(
            P2WPKH_HASH, "peer-1", "BTC", ledger=declaration
        ))), 1)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MigrationTests(unittest.TestCase):

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        cursor = self.connection.cursor()
        # A database from before: no `formal` column, and the tags name only the chain.
        cursor.execute(
            "CREATE TABLE contract_instance (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "address TEXT, ledger TEXT NOT NULL, contract_hash TEXT, "
            "token_id TEXT NOT NULL DEFAULT '', peer_id TEXT NOT NULL, mu_per_unit TEXT, "
            "UNIQUE (address, ledger, contract_hash, token_id, peer_id))"
        )
        migrate.ensure_tables(cursor, ("ledger", "donation_accrual"))
        cursor.executemany(
            "INSERT INTO contract_instance (address, ledger, contract_hash, token_id, "
            "peer_id) VALUES (?, ?, 'h', 'BTC', ?)",
            [("old", "bitcoin", "LOCAL"), ("peer", "ergo", "peer-1"),
             ("simulated", "simulated", "LOCAL")],
        )
        cursor.execute(
            "INSERT INTO donation_accrual (ledger, contract_hash, token_id, owed_native) "
            "VALUES ('bitcoin', 'h', 'BTC', '1234')"
        )
        cursor.execute("INSERT INTO ledger (tag) VALUES ('ergo')")

    def _migrate(self):
        with _configured(**{
            "ledgers.bitcoin.NETWORK": "regtest",
            "ledgers.ergo.GENESIS_BLOCK_ID": MAINNET_GENESIS,
        }):
            migrate.name_the_network_in_ledger_tags(self.connection.cursor())

    def test_rows_of_the_old_tags_are_announced_again_and_debts_are_kept(self):
        self._migrate()
        columns = {row[1] for row in self.connection.execute(
            "PRAGMA table_info(contract_instance)")}
        self.assertIn("formal", columns)
        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT ledger FROM contract_instance")],
            ["simulated"],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT ledger, owed_native FROM donation_accrual").fetchall(),
            [("bitcoin-regtest", "1234")],
        )
        self.assertEqual(
            self.connection.execute("SELECT tag FROM ledger").fetchall(),
            [("ergo-mainnet",)],
        )

    def test_it_runs_once(self):
        self._migrate()
        # A row written after the migration with a chain tag (it cannot happen, but the
        # migration must not touch the database again).
        self.connection.execute(
            "INSERT INTO donation_accrual (ledger, contract_hash, token_id, owed_native) "
            "VALUES ('ergo', 'h', 'ERG', '1')"
        )
        self._migrate()
        self.assertIn(
            ("ergo",),
            self.connection.execute("SELECT ledger FROM donation_accrual").fetchall(),
        )

    def test_a_new_database_is_left_to_create_tables(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        migrate.name_the_network_in_ledger_tags(connection.cursor())
        self.assertEqual(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'contract_instance'"
            ).fetchall(),
            [],
        )


if __name__ == "__main__":
    unittest.main()
