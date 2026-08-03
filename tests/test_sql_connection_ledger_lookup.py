"""check_if_ledger_exists must read the serialized ledger, not a digest.

It selected ``hash`` and then subscripted ``row['id']`` — a column that does not
exist in the ledger table. With an empty table the loop never ran and nothing
broke; as soon as one ledger was stored, every add_contract raised
``IndexError: No item with that key``. That killed the registration of the node's
own payment contract and the storing of a peer's, so payments could not work on
any node that had ever recorded a ledger.
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
class LedgerLookupTests(unittest.TestCase):
    def setUp(self):
        self.ergo = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo chain", formal=b"")
        self.other = celaut_pb2.Contract.Ledger(tags=["other"], prose="Another chain", formal=b"")

    def _lookup(self, stored_rows, ledger_to_check):
        conn = SQLConnection()
        with patch.object(conn, "_execute", return_value=_FakeCursor(stored_rows)) as execute:
            result = conn.check_if_ledger_exists(ledger_to_check=ledger_to_check)
        return result, execute

    def test_a_stored_ledger_does_not_raise(self):
        # The regression: any row at all used to raise IndexError.
        row = {"content": self.ergo.SerializeToString()}
        result, _ = self._lookup([row], self.ergo)
        self.assertEqual(result.prose, "Ergo chain")

    def test_the_serialized_content_column_is_queried(self):
        row = {"content": self.ergo.SerializeToString()}
        _, execute = self._lookup([row], self.ergo)
        query = execute.call_args[0][0]
        self.assertIn("content", query)
        self.assertNotIn("SELECT hash", query)

    def test_a_matching_ledger_is_returned_from_the_database(self):
        # Same chain, richer stored copy: the caller must get the stored one, since
        # add_contract hashes it to key the instance.
        stored = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo chain", formal=b"formal")
        result, _ = self._lookup(
            [{"content": stored.SerializeToString()}],
            celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo chain", formal=b"formal"),
        )
        self.assertEqual(result.formal, b"formal")

    def test_a_non_matching_ledger_falls_back_to_the_input(self):
        result, _ = self._lookup([{"content": self.other.SerializeToString()}], self.ergo)
        self.assertEqual(result.prose, "Ergo chain")

    def test_an_empty_table_falls_back_to_the_input(self):
        result, _ = self._lookup([], self.ergo)
        self.assertEqual(result.prose, "Ergo chain")

    def test_the_match_is_found_past_a_non_matching_row(self):
        rows = [
            {"content": self.other.SerializeToString()},
            {"content": self.ergo.SerializeToString()},
        ]
        result, _ = self._lookup(rows, self.ergo)
        self.assertEqual(result.prose, "Ergo chain")


if __name__ == "__main__":
    unittest.main()
