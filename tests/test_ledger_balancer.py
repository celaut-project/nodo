"""ledger_balancer receives Ledger messages, not strings.

It tracked what it had checked in a ``Set[str]`` and put the ledger straight in,
but ``get_peer_contract_instances`` yields the deserialized ``Contract.Ledger``
message — which protobuf makes unhashable. Every payment attempt died with
``unhashable type: 'Ledger'`` before reaching the ledger, right after the peer had
already issued a deposit token.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.database.sql_connection import SQLConnection
    from src.payment_system.ledger_balancer import ledger_balancer
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ledger_balancer = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LedgerBalancerTests(unittest.TestCase):
    def setUp(self):
        self.ergo = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo chain", formal=b"")
        self.other = celaut_pb2.Contract.Ledger(tags=["other"], prose="Another chain", formal=b"")
        self.script = bytes.fromhex("0008cd02" + "aa" * 32)

    def _balance(self, instances, available=True):
        with patch.object(
            SQLConnection, "check_if_ledger_is_available", return_value=available
        ) as check:
            return list(ledger_balancer(iter(instances))), check

    def test_a_ledger_message_does_not_raise(self):
        # The regression: the message went into a Set[str].
        result, _ = self._balance([(self.script, self.ergo)])
        self.assertEqual(len(result), 1)

    def test_an_available_ledger_is_yielded_unchanged(self):
        [(script, ledger)], _ = self._balance([(self.script, self.ergo)])
        self.assertEqual(script, self.script)
        self.assertEqual(ledger.prose, "Ergo chain")

    def test_an_unavailable_ledger_is_filtered_out(self):
        result, _ = self._balance([(self.script, self.ergo)], available=False)
        self.assertEqual(result, [])

    def test_an_unavailable_ledger_stays_filtered_on_a_repeat(self):
        # The old branch yielded any already-checked ledger without looking at the
        # verdict, so a repeat of an unavailable one slipped through.
        result, _ = self._balance(
            [(self.script, self.ergo), (b"\x01\x02\x03\x04\x05\x06\x07", self.ergo)],
            available=False,
        )
        self.assertEqual(result, [])

    def test_each_ledger_is_checked_once(self):
        other_script = bytes.fromhex("0008cd03" + "bb" * 32)
        result, check = self._balance(
            [(self.script, self.ergo), (other_script, self.ergo)]
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(check.call_count, 1)

    def test_distinct_ledgers_are_checked_separately(self):
        result, check = self._balance([(self.script, self.ergo), (self.script, self.other)])
        self.assertEqual(len(result), 2)
        self.assertEqual(check.call_count, 2)

    def test_nothing_in_nothing_out(self):
        result, check = self._balance([])
        self.assertEqual(result, [])
        self.assertEqual(check.call_count, 0)


if __name__ == "__main__":
    unittest.main()
