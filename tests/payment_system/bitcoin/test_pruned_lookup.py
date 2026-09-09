"""Reading a transaction back on a node that does not keep the whole chain.

`getrawtransaction` can only find an arbitrary confirmed transaction when Core keeps a
`txindex`, and a `txindex` cannot be built on a pruned node. So asking for one quietly
required every operator to keep ~700 GB — for a lookup that never needs it: both callers
pass a txid that came out of a wallet call, so the transaction is always one of this
wallet's own, and the wallet knows it whatever the node keeps.

That is what makes the `service` backend runnable on the kind of arm board a node lives
on, and it was a defect on the `core` backend before that: an operator who pruned their
own bitcoind could be paid, and could not read the OP_RETURN that says who paid them.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.bitcoin.backend import (
        BackendUnavailable,
        ChainBackend,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ChainBackend = None  # type: ignore[assignment]

TXID = "ab" * 32
RAW_HEX = "0200000000010112..."

DECODED = {
    "vout": [
        {"value": 0.00120000,
         "scriptPubKey": {"address": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"}},
        {"value": 0,
         "scriptPubKey": {"asm": "OP_RETURN 6465706f7369742d746f6b656e"}},
    ]
}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PrunedLookupTests(unittest.TestCase):
    def _chain(self, answers):
        chain = ChainBackend(url="http://node", wallet="nodo", auth=None)
        calls = []

        def call(method, params=None, *, wallet_scoped=True):
            calls.append(method)
            answer = answers.get(method)
            if isinstance(answer, Exception):
                raise answer
            return answer

        chain._call = call  # type: ignore[assignment]
        return chain, calls

    def test_the_wallet_is_asked_first_and_no_index_is_needed(self):
        chain, calls = self._chain({
            "gettransaction": {"hex": RAW_HEX},
            "decoderawtransaction": DECODED,
        })
        result = chain.raw_transaction(TXID)

        self.assertNotIn("getrawtransaction", calls,
                         "a pruned node cannot answer that, and never has to")
        self.assertEqual(calls, ["gettransaction", "decoderawtransaction"])
        self.assertEqual(len(result["outputs"]), 2)

    def test_the_outputs_are_the_same_either_way(self):
        # The normalisation the contract reads must not depend on which call answered:
        # one route giving a different shape is a payment proved on one node and not on
        # another.
        wallet_chain, _ = self._chain({
            "gettransaction": {"hex": RAW_HEX}, "decoderawtransaction": DECODED,
        })
        index_chain, _ = self._chain({
            "gettransaction": BackendUnavailable("no such transaction"),
            "getrawtransaction": DECODED,
        })
        self.assertEqual(
            wallet_chain.raw_transaction(TXID), index_chain.raw_transaction(TXID)
        )

    def test_a_transaction_this_wallet_does_not_know_falls_back_to_the_index(self):
        # A node that kept one can still answer; one that did not says so, which is the
        # honest outcome rather than a payment silently unprovable.
        chain, calls = self._chain({
            "gettransaction": BackendUnavailable("Invalid or non-wallet transaction id"),
            "getrawtransaction": DECODED,
        })
        chain.raw_transaction(TXID)
        self.assertEqual(calls, ["gettransaction", "getrawtransaction"])

    def test_a_wallet_entry_with_no_hex_falls_back_too(self):
        # Older Core versions do not return the raw hex on `gettransaction`.
        chain, calls = self._chain({
            "gettransaction": {"confirmations": 3},
            "getrawtransaction": DECODED,
        })
        chain.raw_transaction(TXID)
        self.assertIn("getrawtransaction", calls)

    def test_a_pruned_node_that_knows_nothing_raises_rather_than_answering_empty(self):
        # "I could not look" and "it paid nothing" are different answers, and treating
        # the second as the first rejects an honest payment already on-chain.
        chain, _calls = self._chain({
            "gettransaction": BackendUnavailable("Invalid or non-wallet transaction id"),
            "getrawtransaction": BackendUnavailable(
                "No such mempool transaction. Use -txindex"
            ),
        })
        with self.assertRaises(BackendUnavailable):
            chain.raw_transaction(TXID)


if __name__ == "__main__":
    unittest.main()
