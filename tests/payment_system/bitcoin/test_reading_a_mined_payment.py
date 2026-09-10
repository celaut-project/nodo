"""Reading a payment that is already on-chain, on a node that keeps no tx index.

Every one of these guards the same direction of error. The receiver is the one holding
the money -- an incoming payment landed in its own wallet -- so answering "no payment
arrived" when the real answer is "I could not find it" keeps somebody else's BTC and
credits them nothing. There is no reconciliation pass behind it: `Payable` is asked
once, `COMMUNICATION_ATTEMPTS` defaults to 1, and an `unacknowledged` row is only ever
displayed.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.payment_system.contracts.bitcoin import esplora
    from src.payment_system.contracts.bitcoin.backend import (
        BackendUnavailable,
        ChainBackend,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    esplora = None  # type: ignore[assignment]

ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
DECODED = {"vout": [{"value": 0.001, "n": 0, "scriptPubKey": {"hex": "0014" + "11" * 20}}]}


def _core(answers):
    """A Core backend whose RPC calls are answered from ``answers``, keyed by method."""
    chain = ChainBackend("http://example.invalid", wallet="w")
    calls = []

    def _call(method, params=None, *, wallet_scoped=True):
        calls.append((method, list(params or []), wallet_scoped))
        answer = answers.get(method)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(list(params or []))
        return answer

    chain._call = _call  # type: ignore[assignment]
    return chain, calls


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MinedTransactionTests(unittest.TestCase):
    """`getrawtransaction` is the wrong RPC for a transaction that has been mined.

    Core answers it from the mempool, and otherwise only with `-txindex=1` or a
    blockhash. Every txid that reaches `raw_transaction` has been mined -- they come
    from `list_received` filtered by confirmations, and from `list_transactions` -- so
    on a default bitcoind the plain call returns error -5 for exactly the payments the
    validator has to read.
    """

    def test_it_is_read_through_the_wallet_not_the_tx_index(self):
        chain, calls = _core({"gettransaction": {"decoded": DECODED}})
        outputs = chain.raw_transaction("tx-1")["outputs"]
        self.assertEqual(len(outputs), 1)
        self.assertEqual([method for method, _, _ in calls], ["gettransaction"])
        # Verbose, or Core sends no `decoded` at all.
        self.assertEqual(calls[0][1], ["tx-1", True, True])

    def test_without_a_decoded_body_the_block_is_named_so_no_index_is_needed(self):
        chain, calls = _core({
            "gettransaction": {"blockhash": "block-9"},
            "getrawtransaction": DECODED,
        })
        self.assertEqual(len(chain.raw_transaction("tx-1")["outputs"]), 1)
        raw = [call for call in calls if call[0] == "getrawtransaction"][0]
        self.assertEqual(raw[1], ["tx-1", True, "block-9"])
        # Not wallet-scoped: `getrawtransaction` is a node call, not a wallet one.
        self.assertFalse(raw[2])

    def test_a_core_too_old_for_verbose_is_asked_again_without_it(self):
        # The refusal is about the parameters, not about the node, and giving up here
        # would reject a real payment.
        answers = {"gettransaction": None, "getrawtransaction": DECODED}
        seen = []

        def gettransaction(params):
            seen.append(params)
            if len(params) > 2:
                raise BackendUnavailable("bitcoind gettransaction: wrong params")
            return {"blockhash": "block-9"}

        answers["gettransaction"] = gettransaction
        chain, _ = _core(answers)
        self.assertEqual(len(chain.raw_transaction("tx-1")["outputs"]), 1)
        self.assertEqual([len(params) for params in seen], [3, 2])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EsploraPaginationTests(unittest.TestCase):
    """One page of address history is not the history.

    `/address/:addr/txs` returns the newest page only, so on a node paid by several
    peers a deposit that has slipped past it would read as "no confirmed transaction
    carries the token" -- for a payment already in this node's wallet.
    """

    def _pages(self, first, chain_pages):
        responses = {
            "/blocks/tip/height": 1_000_000,
            f"/address/{ADDRESS}/txs": first,
        }
        for last_seen, page in chain_pages.items():
            responses[f"/address/{ADDRESS}/txs/chain/{last_seen}"] = page
        backend = esplora.EsploraBackend("https://example.invalid/api")
        asked = []

        def _get(path):
            asked.append(path)
            return responses.get(path)

        backend._get = _get  # type: ignore[assignment]
        return backend, asked

    @staticmethod
    def _confirmed(txid, height=900_000):
        return {"txid": txid, "status": {"confirmed": True, "block_height": height}}

    def test_a_full_page_is_followed_to_the_next_one(self):
        first = [self._confirmed(f"tx-{i}") for i in range(esplora.PAGE_SIZE)]
        backend, asked = self._pages(first, {f"tx-{esplora.PAGE_SIZE - 1}": [self._confirmed("older")]})
        found = backend.list_received(ADDRESS, 1)[0]["txids"]
        self.assertIn("older", found)
        self.assertEqual(len(found), esplora.PAGE_SIZE + 1)
        self.assertIn(f"/address/{ADDRESS}/txs/chain/tx-{esplora.PAGE_SIZE - 1}", asked)

    def test_a_short_page_is_the_last_one(self):
        backend, asked = self._pages([self._confirmed("only")], {})
        self.assertEqual(backend.list_received(ADDRESS, 1), [{"txids": ["only"]}])
        self.assertEqual(asked.count(f"/address/{ADDRESS}/txs"), 1)
        self.assertFalse([path for path in asked if "/chain/" in path])

    def test_the_cursor_is_never_a_mempool_transaction(self):
        # `/txs/chain` walks confirmed history, and the first page carries the mempool
        # first -- so it can be longer than a page without there being another one.
        first = (
            [{"txid": "pending", "status": {"confirmed": False}}]
            + [self._confirmed(f"tx-{i}") for i in range(esplora.PAGE_SIZE)]
        )
        backend, asked = self._pages(first, {f"tx-{esplora.PAGE_SIZE - 1}": []})
        backend.list_received(ADDRESS, 1)
        followed = [path for path in asked if "/chain/" in path]
        self.assertEqual(followed, [f"/address/{ADDRESS}/txs/chain/tx-{esplora.PAGE_SIZE - 1}"])

    def test_only_deep_enough_transactions_are_reported(self):
        first = [self._confirmed("deep", 900_000), self._confirmed("shallow", 1_000_000)]
        backend, _ = self._pages(first, {})
        self.assertEqual(backend.list_received(ADDRESS, 3), [{"txids": ["deep"]}])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UnderpaidCandidateTests(unittest.TestCase):
    """One transaction paying too little is not a verdict on the others.

    A token can appear in more than one confirmed transaction: on a `Payable` that went
    unacknowledged the payer records it and moves on, and the per-script walk can pay
    again. Deciding on the first candidate would reject a payment another transaction
    covers in full -- and the branch just above, for a transaction that could not be
    read at all, already keeps looking for exactly this reason.
    """

    SCRIPT = bytes.fromhex("0014" + "22" * 20)

    def _validate(self, transactions, expected_sat):
        from protos import celaut_pb2
        from src.payment_system.contracts.bitcoin import interface

        ledger = celaut_pb2.Contract.Ledger()
        ledger.tags.append(interface.LEDGER)

        chain = mock.MagicMock()
        chain.list_received.return_value = [{"txids": list(transactions)}]
        chain.raw_transaction.side_effect = lambda tx_id: transactions[tx_id]

        with mock.patch.object(interface, "backend", return_value=chain), \
                mock.patch.object(interface, "get_wallet_script", return_value=self.SCRIPT), \
                mock.patch.object(interface, "get_wallet_address", return_value=ADDRESS), \
                mock.patch.object(interface, "MIN_CONFIRMATIONS", lambda: 1), \
                mock.patch.object(interface.rate, "mu_to_satoshi", lambda mu: expected_sat):
            return interface.payment_process_validator(
                amount=1_000, token="tok", ledger=ledger, script=self.SCRIPT,
            )

    def _tx(self, paid_sat, token="tok"):
        # The backend's normalised shape (`script_hex` / `value_sat` / `op_return`),
        # not any one backend's JSON -- which is the whole point of normalising.
        return {"outputs": [
            {"script_hex": self.SCRIPT.hex(), "value_sat": paid_sat},
            {"script_hex": "6a", "value_sat": 0, "op_return": token.encode()},
        ]}

    def test_a_later_transaction_can_still_cover_the_payment(self):
        self.assertTrue(self._validate(
            {"short": self._tx(400), "full": self._tx(1_000)}, expected_sat=1_000
        ))

    def test_every_candidate_falling_short_is_still_a_no(self):
        self.assertFalse(self._validate(
            {"short": self._tx(400), "shorter": self._tx(10)}, expected_sat=1_000
        ))


if __name__ == "__main__":
    unittest.main()
