"""``nodo peers`` used to answer "which contract does this peer charge through?"
with a single hardcoded Ergo P2PK lookup (see issue #231): any peer using a
different contract or ledger looked exactly like a peer with no contract at
all, and a peer with several instances only ever showed one.

``get_peer_payment_contracts`` replaces that with a per-peer enumeration of
every ``contract_instance`` row. The ledger comes off the row as its tag: it used
to be stored as the sha3 of a serialized ``Contract.Ledger`` and recovered here
with a second query and a protobuf parse *per row*, to arrive at a string the row
could have held in the first place (issue #82).
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GetPeerPaymentContractsTests(unittest.TestCase):
    def setUp(self):
        self.conn = SQLConnection()

    def _contracts(self, rows):
        with patch.object(self.conn, "_execute", return_value=_FakeCursor(rows)) as execute:
            result = self.conn.get_peer_payment_contracts("peer-1")
        return result, execute

    def test_no_contracts_returns_empty_list(self):
        result, _ = self._contracts([])
        self.assertEqual(result, [])

    def test_reads_the_ledger_tag_and_rate_off_the_row(self):
        row = {
            "contract_hash": "abc123",
            "ledger": "ergo",
            "address": "0008cd0392",
            "token_id": "ERG",
            "mu_per_unit": "9999999999999999438119489974413630815797154428513196965888",
        }
        result, execute = self._contracts([row])

        self.assertEqual(len(result), 1)
        contract = result[0]
        self.assertEqual(contract["contract_hash"], "abc123")
        self.assertEqual(contract["ledger_tag"], "ergo")
        self.assertEqual(contract["address"], "0008cd0392")
        self.assertEqual(contract["token_id"], "ERG")
        self.assertEqual(
            contract["mu_per_unit"],
            9999999999999999438119489974413630815797154428513196965888,
        )
        # One query for the whole listing: no per-row ledger resolution any more.
        self.assertEqual(execute.call_count, 1)

    def test_invalid_rate_becomes_none(self):
        row = {
            "contract_hash": "abc123",
            "ledger": "ergo",
            "address": "addr",
            "token_id": "ERG",
            "mu_per_unit": None,
        }
        result, _ = self._contracts([row])
        self.assertIsNone(result[0]["mu_per_unit"])

    def test_multiple_instances_for_one_peer_are_all_returned(self):
        # The old code could only ever show one contract per peer; a peer with
        # several must not get truncated to the first.
        rows = [
            {"contract_hash": "c1", "ledger": "ergo", "address": "a1",
             "token_id": "ERG", "mu_per_unit": "1"},
            {"contract_hash": "c2", "ledger": "bitcoin", "address": "a2",
             "token_id": "BTC", "mu_per_unit": "2"},
        ]
        result, _ = self._contracts(rows)

        self.assertEqual([c["contract_hash"] for c in result], ["c1", "c2"])
        self.assertEqual([c["ledger_tag"] for c in result], ["ergo", "bitcoin"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ContractInstanceRateIsUpsertedTests(unittest.TestCase):
    """A peer re-advertises its rate on every refresh; the row has to follow it.

    `add_contract` used INSERT OR IGNORE, which froze `mu_per_unit` at whatever a
    peer announced the first time we saw it. Converting MU with a stale rate
    misprices delegation and, on the payment path, gets a deposit rejected with
    the money already on-chain.
    """

    STATEMENT = (
        "INSERT INTO contract_instance "
        "(address, ledger, contract_hash, token_id, peer_id, mu_per_unit) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT (address, ledger, contract_hash, token_id, peer_id) "
        "DO UPDATE SET mu_per_unit = excluded.mu_per_unit"
    )

    def test_re_registering_a_peer_updates_its_rate(self):
        import sqlite3
        from src.database.migrate import TABLES

        db = sqlite3.connect(":memory:")
        db.execute(TABLES["contract_instance"])
        row = ("addr", "ergo", "contract", "ERG", "peer-1")
        db.execute(self.STATEMENT, (*row, "1000000000"))
        db.execute(self.STATEMENT, (*row, "2000000000"))

        stored = db.execute("SELECT mu_per_unit FROM contract_instance").fetchall()
        self.assertEqual(stored, [("2000000000",)])

    def test_two_assets_on_one_contract_keep_two_rates(self):
        """The bug the asset dimension exists to fix.

        On Ergo one P2PK contract is paid in ERG and in every EIP-4 token at the same
        address: same script, same address, same contract_hash. Keyed without the asset
        the two rates land in one row and the second silently replaces the first -- so
        every peer converts ERG amounts at the token's rate, and nothing raises.
        """
        import sqlite3
        from src.database.migrate import TABLES

        db = sqlite3.connect(":memory:")
        db.execute(TABLES["contract_instance"])
        token = "ab" * 32
        db.execute(self.STATEMENT, ("addr", "ergo", "contract", "ERG", "LOCAL", "1"))
        db.execute(self.STATEMENT, ("addr", "ergo", "contract", token, "LOCAL", "20000000"))

        stored = dict(db.execute(
            "SELECT token_id, mu_per_unit FROM contract_instance"
        ).fetchall())
        self.assertEqual(stored, {"ERG": "1", token: "20000000"})


if __name__ == "__main__":
    unittest.main()
