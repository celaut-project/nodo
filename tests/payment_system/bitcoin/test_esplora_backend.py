"""Being paid in BTC without running anything, and never pretending it can pay.

Ergo's posture is a remote public node plus a local key, so an operator runs no Ergo
infrastructure. Bitcoin Core over RPC is the opposite -- Core signs, so it has to be a
node you would hand your wallet to -- and requiring that of anyone who merely wants to
*receive* BTC is a heavier ask than this project makes anywhere else.

This backend closes that gap for the receiving side only, and the tests that matter are
the refusals: a backend that cannot sign must say so where the payer can act on it,
not somewhere in the middle of a payment.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.bitcoin import esplora
    from src.payment_system.contracts.bitcoin.backend import BackendUnavailable
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    esplora = None  # type: ignore[assignment]

ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"


def _backend(responses):
    """An Esplora backend whose HTTP reads are answered from ``responses``."""
    chain = esplora.EsploraBackend("https://example.invalid/api")
    chain._get = lambda path: responses.get(path)  # type: ignore[assignment]
    return chain


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReadOnlyTests(unittest.TestCase):

    def test_it_declares_that_it_cannot_pay(self):
        # The payer reads this, not an exception: funding is the selection, so a system
        # that cannot sign has no funding and the walk moves on.
        self.assertFalse(esplora.EsploraBackend("x").can_pay)

    def test_every_write_refuses_with_the_reason_and_the_fix(self):
        chain = esplora.EsploraBackend("x")
        for call in (
            lambda: chain.new_address(),
            lambda: chain.send_to(ADDRESS, 1_000),
            lambda: chain.send_many([(ADDRESS, 1_000)]),
        ):
            with self.subTest(call=call):
                with self.assertRaises(BackendUnavailable) as raised:
                    call()
                self.assertIn("read-only", str(raised.exception))
                self.assertIn("BACKEND: core", str(raised.exception))

    def test_the_balance_is_the_addresss_confirmed_funds(self):
        chain = _backend({
            f"/address/{ADDRESS}": {
                "chain_stats": {"funded_txo_sum": 150_000, "spent_txo_sum": 50_000},
                # Unconfirmed money is not a balance: `chain_stats` excludes it, and
                # this must not start adding it in.
                "mempool_stats": {"funded_txo_sum": 999_999, "spent_txo_sum": 0},
            }
        })
        with mock.patch.object(esplora, "_receiving_address", return_value=ADDRESS):
            self.assertEqual(chain.get_balance(), 100_000)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfirmationTests(unittest.TestCase):
    """Esplora reports the block a transaction landed in, not how deep it is."""

    def test_depth_is_derived_against_the_tip(self):
        chain = _backend({
            "/blocks/tip/height": 900_010,
            "/tx/tx-1": {"status": {"confirmed": True, "block_height": 900_000}},
        })
        self.assertEqual(chain.tx_status("tx-1")["confirmations"], 11)

    def test_unconfirmed_is_zero_not_missing(self):
        chain = _backend({
            "/blocks/tip/height": 900_010,
            "/tx/tx-1": {"status": {"confirmed": False}},
        })
        self.assertEqual(chain.tx_status("tx-1")["confirmations"], 0)

    def test_a_tip_it_could_not_read_is_zero_confirmations(self):
        """"I could not tell" must never read as "deep enough".

        Answering with a depth derived from a missing tip would credit a payment this
        node cannot see the finality of.
        """
        chain = _backend({
            "/blocks/tip/height": None,
            "/tx/tx-1": {"status": {"confirmed": True, "block_height": 900_000}},
        })
        self.assertEqual(chain.tx_status("tx-1")["confirmations"], 0)

    def test_a_transaction_that_is_gone_is_zero_rather_than_an_error(self):
        # Esplora stops reporting a transaction that left the chain; the payer's wait
        # treats that as "not yet" and its own timeout bounds it.
        chain = _backend({"/blocks/tip/height": 900_010, "/tx/tx-1": None})
        self.assertEqual(chain.tx_status("tx-1")["confirmations"], 0)

    def test_only_deep_enough_transactions_are_listed_as_received(self):
        chain = _backend({
            "/blocks/tip/height": 900_010,
            f"/address/{ADDRESS}/txs": [
                {"txid": "deep", "status": {"confirmed": True, "block_height": 900_000}},
                {"txid": "shallow", "status": {"confirmed": True, "block_height": 900_010}},
                {"txid": "mempool", "status": {"confirmed": False}},
            ],
        })
        self.assertEqual(chain.list_received(ADDRESS, 3), [{"txids": ["deep"]}])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NormalisedOutputTests(unittest.TestCase):
    """The contract reads one shape, whichever backend produced it."""

    def test_outputs_carry_satoshi_and_the_raw_script(self):
        chain = _backend({
            "/tx/tx-1": {"vout": [
                {"scriptpubkey": "0014" + "11" * 20, "value": 1_500,
                 "scriptpubkey_type": "v0_p2wpkh"},
            ]}
        })
        [output] = chain.raw_transaction("tx-1")["outputs"]
        self.assertEqual(output["value_sat"], 1_500)
        self.assertEqual(output["script_hex"], "0014" + "11" * 20)
        self.assertIsNone(output["op_return"])

    def test_an_op_return_payload_is_read_out_of_the_raw_script(self):
        # Core reports an OP_RETURN as an `asm` string and Esplora as a hex script, so
        # the payload is decoded here rather than in the contract.
        token = b"deposit-token-1"
        script = "6a" + f"{len(token):02x}" + token.hex()
        chain = _backend({
            "/tx/tx-1": {"vout": [
                {"scriptpubkey": script, "value": 0, "scriptpubkey_type": "op_return"},
            ]}
        })
        [output] = chain.raw_transaction("tx-1")["outputs"]
        self.assertEqual(output["op_return"], token)

    def test_a_pushdata_op_return_is_read_too(self):
        token = b"x" * 80
        script = "6a4c" + f"{len(token):02x}" + token.hex()
        self.assertEqual(esplora._op_return_payload(script), token)

    def test_a_script_that_is_not_an_op_return_carries_no_payload(self):
        self.assertIsNone(esplora._op_return_payload("0014" + "11" * 20))
        self.assertIsNone(esplora._op_return_payload("not-hex"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FeeEstimateTests(unittest.TestCase):

    def test_the_nearest_target_at_or_above_the_one_asked_for_is_used(self):
        # A lower target would promise a confirmation nobody estimated.
        chain = _backend({"/fee-estimates": {"1": 20.0, "3": 8.0, "6": 5.0, "25": 2.0}})
        self.assertEqual(chain.estimate_fee_rate(6), 5.0)
        self.assertEqual(chain.estimate_fee_rate(4), 5.0)
        self.assertEqual(chain.estimate_fee_rate(1), 20.0)

    def test_no_estimate_is_none_rather_than_a_guess(self):
        self.assertIsNone(_backend({"/fee-estimates": None}).estimate_fee_rate(6))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfigurationTests(unittest.TestCase):

    def _reason(self, url="https://example.invalid/api", address=ADDRESS):
        values = {
            "ledgers.bitcoin.ESPLORA_URL": url,
            "ledgers.bitcoin.payments.RECEIVING_ADDRESS": address,
        }
        with mock.patch.object(
            esplora.ConfigManager(), "get",
            side_effect=lambda key, default=None: values.get(key, default),
        ):
            return esplora.configuration_reason()

    def test_a_configured_backend_is_usable(self):
        self.assertIsNone(self._reason())

    def test_no_url_is_named(self):
        self.assertIn("ESPLORA_URL", self._reason(url=""))

    def test_a_read_only_backend_needs_the_address_configured_by_hand(self):
        """It cannot ask a node for one, and inventing one would strand payments.

        An address minted after peers were told a different one is an address nobody
        pays to.
        """
        reason = self._reason(address="")
        self.assertIn("RECEIVING_ADDRESS", reason)
        self.assertIn("read-only", reason)


if __name__ == "__main__":
    unittest.main()
