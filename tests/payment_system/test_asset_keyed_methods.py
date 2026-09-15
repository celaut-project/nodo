"""The regression that matters: two rates under one contract_hash, both surviving.

An Ergo P2PK contract is paid in ERG and in every EIP-4 token at the same address --
same script, same address, same `contract_hash`. `mu_per_unit` lives on the
`contract_instance` row and `add_contract` upserts it, so before `token_id` was part of
that row's identity the two methods were one row: the second rate silently replaced the
first, and every peer then converted ERG amounts at the token's rate. Nothing raised,
on either side.

These tests go through real SQL rather than a stubbed cursor, because what is under test
is the table's unique key and the `ON CONFLICT` target that has to match it.
"""
import sqlite3
import unittest
from hashlib import sha3_256

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
    from src.utils.contract_xattrs import (
        set_address,
        set_contract_type,
        set_script,
        set_token_id,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]

CONTRACT = "proveDlog(decodePoint())"
CONTRACT_HASH = sha3_256(CONTRACT.encode("utf-8")).hexdigest()
SCRIPT = bytes.fromhex("0008cd03" + "77" * 32)
ADDRESS = "9walletADDR"
TOKEN = "ab" * 32


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AssetKeyedMethodTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        cursor = self.connection.cursor()
        migrate.ensure_tables(cursor, ("contract_instance", "ledger"))

        self.sql = SQLConnection()
        saved = SQLConnection._connection
        SQLConnection._connection = self.connection
        self.addCleanup(lambda: setattr(SQLConnection, "_connection", saved))

        self.ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo", formal=b"")

    def _advertise(self, asset, rate, script=SCRIPT, peer_id="LOCAL"):
        """One payment method, as `add_contract` receives it off the wire."""
        contract = celaut_pb2.Contract(ledger=self.ledger)
        set_token_id(contract, asset)
        set_script(contract, script)
        set_contract_type(contract, CONTRACT.encode("utf-8"))
        set_address(contract, ADDRESS)
        self.sql.add_contract(contract=contract, peer_id=peer_id, mu_per_unit=rate)
        return contract

    def _rows(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT token_id, mu_per_unit FROM contract_instance ORDER BY id"
        ).fetchall()]

    def test_two_assets_on_one_contract_keep_two_independent_rates(self):
        self._advertise("ERG", 1_000_000_000)
        self._advertise(TOKEN, 20_000_000)
        self.assertEqual(self._rows(), [
            {"token_id": "ERG", "mu_per_unit": "1000000000"},
            {"token_id": TOKEN, "mu_per_unit": "20000000"},
        ])

    def test_re_advertising_one_asset_updates_only_that_rate(self):
        # A peer re-advertises on every refresh, so this row has to be an upsert --
        # and the upsert must land on the method, not on the contract.
        self._advertise("ERG", 1_000_000_000)
        self._advertise(TOKEN, 20_000_000)
        self._advertise(TOKEN, 25_000_000)
        self.assertEqual(self._rows(), [
            {"token_id": "ERG", "mu_per_unit": "1000000000"},
            {"token_id": TOKEN, "mu_per_unit": "25000000"},
        ])

    def test_the_asset_round_trips_back_out(self):
        self._advertise("ERG", 1)
        self._advertise(TOKEN, 2)
        self.assertEqual(
            [(script, asset) for script, _ledger, asset
             in self.sql.get_peer_contract_instances(CONTRACT_HASH)],
            [(SCRIPT, "ERG"), (SCRIPT, TOKEN)],
        )

    def test_one_method_can_be_read_without_the_others(self):
        # What the payer does: it settles in one asset, and the rows of another are the
        # right address for the wrong money.
        self._advertise("ERG", 1)
        self._advertise(TOKEN, 2)
        self.assertEqual(
            [asset for _script, _ledger, asset
             in self.sql.get_peer_contract_instances(CONTRACT_HASH, asset=TOKEN)],
            [TOKEN],
        )

    def test_a_peers_methods_are_enumerated_with_their_assets_and_rates(self):
        # `nodo peers` and `mu_conversion` both read this. Without the asset the two
        # rows read as one contract advertised twice at two different rates, which
        # `_rates_by_payment_system` refuses as a contradiction -- so a peer payable in
        # two currencies became payable in none.
        self._advertise("ERG", 1_000_000_000, peer_id="peer-1")
        self._advertise(TOKEN, 20_000_000, peer_id="peer-1")
        self.assertEqual(
            [(c["token_id"], c["mu_per_unit"])
             for c in self.sql.get_peer_payment_contracts("peer-1")],
            [("ERG", 1_000_000_000), (TOKEN, 20_000_000)],
        )

    def test_ergs_row_is_what_it_always_was(self):
        # The asset dimension must not change the native unit's identity: a node that
        # configures no token stores exactly the row it stored before this existed.
        self._advertise("ERG", 1_000_000_000)
        [row] = [dict(r) for r in self.connection.execute(
            "SELECT address, contract_hash, token_id, mu_per_unit, peer_id "
            "FROM contract_instance"
        ).fetchall()]
        self.assertEqual(row, {
            "address": SCRIPT.hex(),
            "contract_hash": CONTRACT_HASH,
            "token_id": "ERG",
            "mu_per_unit": "1000000000",
            "peer_id": "LOCAL",
        })

    def test_the_same_id_in_either_case_is_one_asset(self):
        """A token id is hex, and a peer may advertise it in either case.

        Stored as advertised, the same asset becomes two rows with two rates -- and the
        method keyed on one is simply not found by the other. The symptom is not a wrong
        payment but a peer quietly unpayable in that token, which nobody debugs.
        """
        self._advertise(TOKEN.upper(), 20_000_000)
        self._advertise(TOKEN, 25_000_000)
        self.assertEqual(self._rows(), [{"token_id": TOKEN, "mu_per_unit": "25000000"}])

    def test_a_native_symbol_keeps_its_case(self):
        # "ERG" is a reserved symbol, not an id, and it travels as advertised.
        self._advertise("ERG", 1)
        self.assertEqual(self._rows()[0]["token_id"], "ERG")

    def test_two_peers_advertising_the_same_asset_are_two_rows(self):
        self._advertise(TOKEN, 1, peer_id="peer-1")
        self._advertise(TOKEN, 2, peer_id="peer-2")
        self.assertEqual(len(self._rows()), 2)

    def test_two_wallets_of_one_peer_in_one_asset_are_two_rows(self):
        other_script = bytes.fromhex("0008cd02" + "11" * 32)
        self._advertise(TOKEN, 1)
        self._advertise(TOKEN, 1, script=other_script)
        self.assertEqual(len(self._rows()), 2)


if __name__ == "__main__":
    unittest.main()
