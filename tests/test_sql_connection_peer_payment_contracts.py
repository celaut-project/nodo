"""``nodo peers`` used to answer "which contract does this peer charge through?"
with a single hardcoded Ergo P2PK lookup (see issue #231): any peer using a
different contract or ledger looked exactly like a peer with no contract at
all, and a peer with several instances only ever showed one.

``get_peer_payment_contracts`` replaces that with a per-peer enumeration of
every ``contract_instance`` row, resolving each one's ledger hash to its tag
(e.g. ``"ergo"``) the same way the payment path already does.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
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
        self.ergo = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo chain", formal=b"")

    def test_no_contracts_returns_empty_list(self):
        with patch.object(self.conn, "_execute", return_value=_FakeCursor([])):
            result = self.conn.get_peer_payment_contracts("peer-1")
        self.assertEqual(result, [])

    def test_resolves_ledger_tag_and_gas_price(self):
        instance_row = {
            "contract_hash": "abc123",
            "ledger_hash": "deadbeef",
            "address": "0008cd0392",
            "gas_price": "9999999999999999438119489974413630815797154428513196965888",
        }
        ledger_row = {"content": self.ergo.SerializeToString()}

        with patch.object(
            self.conn, "_execute",
            side_effect=[_FakeCursor([instance_row]), _FakeCursor([ledger_row])],
        ):
            result = self.conn.get_peer_payment_contracts("peer-1")

        self.assertEqual(len(result), 1)
        contract = result[0]
        self.assertEqual(contract["contract_hash"], "abc123")
        self.assertEqual(contract["ledger_tag"], "ergo")
        self.assertEqual(contract["address"], "0008cd0392")
        self.assertEqual(
            contract["gas_price"],
            9999999999999999438119489974413630815797154428513196965888,
        )

    def test_missing_ledger_row_falls_back_to_raw_hash(self):
        # The ledger row can't always be resolved (e.g. mid-migration data);
        # surfacing the raw hash beats hiding the contract entirely.
        instance_row = {
            "contract_hash": "abc123",
            "ledger_hash": "deadbeef",
            "address": "addr",
            "gas_price": "5",
        }
        with patch.object(
            self.conn, "_execute",
            side_effect=[_FakeCursor([instance_row]), _FakeCursor([])],
        ):
            result = self.conn.get_peer_payment_contracts("peer-1")

        self.assertEqual(result[0]["ledger_tag"], "deadbeef")

    def test_invalid_gas_price_becomes_none(self):
        instance_row = {
            "contract_hash": "abc123",
            "ledger_hash": "deadbeef",
            "address": "addr",
            "gas_price": None,
        }
        with patch.object(
            self.conn, "_execute",
            side_effect=[_FakeCursor([instance_row]), _FakeCursor([])],
        ):
            result = self.conn.get_peer_payment_contracts("peer-1")

        self.assertIsNone(result[0]["gas_price"])

    def test_multiple_instances_for_one_peer_are_all_returned(self):
        # The old code could only ever show one contract per peer; a peer with
        # several must not get truncated to the first.
        rows = [
            {"contract_hash": "c1", "ledger_hash": "l1", "address": "a1", "gas_price": "1"},
            {"contract_hash": "c2", "ledger_hash": "l2", "address": "a2", "gas_price": "2"},
        ]
        with patch.object(
            self.conn, "_execute",
            side_effect=[_FakeCursor(rows), _FakeCursor([]), _FakeCursor([])],
        ):
            result = self.conn.get_peer_payment_contracts("peer-1")

        self.assertEqual([c["contract_hash"] for c in result], ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
