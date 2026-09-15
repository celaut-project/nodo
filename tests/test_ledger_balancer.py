"""ledger_balancer works in ledger tags.

It once tracked what it had checked in a ``Set[str]`` while
``get_peer_contract_instances`` yielded a deserialized ``Contract.Ledger`` message,
which protobuf makes unhashable: every payment attempt died with
``unhashable type: 'Ledger'`` right after the peer had issued a deposit token. That
was patched by hashing the message to key the dict -- and the real answer, taken in
issue #82, is that the message never had to be here at all. A stored instance now
carries the chain's TAG, which is a string, is hashable, and is the only part of the
ledger anything downstream reads.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.database.sql_connection import SQLConnection
    from src.payment_system.ledger_balancer import ledger_balancer
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ledger_balancer = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LedgerBalancerTests(unittest.TestCase):
    def setUp(self):
        self.ergo = "ergo"
        self.other = "bitcoin"
        self.script = bytes.fromhex("0008cd02" + "aa" * 32)

    def _balance(self, instances, available=True):
        with patch.object(
            SQLConnection, "check_if_ledger_is_available", return_value=available
        ) as check:
            return list(ledger_balancer(iter(instances))), check

    def test_an_available_ledger_is_yielded_unchanged(self):
        [(script, ledger, asset)], _ = self._balance([(self.script, self.ergo, "ERG")])
        self.assertEqual(script, self.script)
        self.assertEqual(ledger, "ergo")
        # The asset rides through untouched: whether a *ledger* is reachable says
        # nothing about which of its assets a payment is in, and dropping it would
        # leave the payer unable to tell two methods of one contract apart.
        self.assertEqual(asset, "ERG")

    def test_the_tag_is_what_availability_is_asked_about(self):
        _, check = self._balance([(self.script, self.ergo, "ERG")])
        self.assertEqual(check.call_args.kwargs, {"ledger": "ergo"})

    def test_an_unavailable_ledger_is_filtered_out(self):
        result, _ = self._balance([(self.script, self.ergo, "ERG")], available=False)
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
