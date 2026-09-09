"""The deposit token rides in an `OP_RETURN`, and that is what ties a payment to it.

Bitcoin has no register to write a token into, so this is the translation of Ergo's
`R4`: a static receiving address plus a data output carrying the token. The rules the
validator has to keep are the same ones Ergo's keeps, and each of them is a way an
honest payment could otherwise be rejected with the money already on-chain.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.payment_system.contracts.bitcoin import interface as btc
    from src.utils.bitcoin_units import script_pubkey_from_address
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    btc = None  # type: ignore[assignment]

ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
OTHER = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
LEDGER = None if IMPORT_ERROR else celaut_pb2.Contract.Ledger(tags=["bitcoin"])
TOKEN = "deposit-token-1"


def _tx(token=TOKEN, paid_to=ADDRESS, amount_sat=1_000, extra_outputs=()):
    """A decoded transaction as `getrawtransaction ... true` returns one."""
    outputs = []
    if token is not None:
        outputs.append({
            "value": 0,
            "scriptPubKey": {"type": "nulldata", "asm": f"OP_RETURN {token.encode().hex()}"},
        })
    if paid_to is not None:
        outputs.append({
            "value": str(Decimal(amount_sat) / 100_000_000),
            "scriptPubKey": {
                "type": "witness_v0_keyhash",
                "hex": script_pubkey_from_address(paid_to).hex(),
            },
        })
    outputs.extend(extra_outputs)
    return {"vout": outputs}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OpReturnValidationTests(unittest.TestCase):

    def _validate(self, amount_mu, transactions, *, mu_per_satoshi=1, address=ADDRESS):
        chain = mock.Mock()
        chain.list_received.return_value = [{"txids": list(transactions)}]
        chain.raw_transaction.side_effect = lambda tx_id: transactions[tx_id]
        with mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "get_wallet_address", return_value=address), \
                mock.patch.object(btc.rate, "mu_per_satoshi",
                                  return_value=Decimal(mu_per_satoshi)), \
                mock.patch.object(btc, "NETWORK", lambda: "mainnet"), \
                mock.patch.object(btc, "MIN_CONFIRMATIONS", lambda: 1):
            return btc.payment_process_validator(
                amount=amount_mu, token=TOKEN, ledger=LEDGER,
                script=script_pubkey_from_address(address),
            )

    def test_the_token_round_trips_through_the_op_return(self):
        self.assertTrue(self._validate(1_000, {"tx-1": _tx()}))

    def test_more_than_asked_for_is_accepted(self):
        """The rule Ergo's validator documents, and for the same reason.

        The payer converts our MU figure from its own scale and has to round down to a
        whole MU of ours, so a correct payment routinely carries a little more than the
        credit it asks for. Demanding equality would reject payments already on-chain.
        """
        self.assertTrue(self._validate(1_000, {"tx-1": _tx(amount_sat=1_500)}))

    def test_the_right_amount_of_the_wrong_token_is_rejected(self):
        self.assertFalse(self._validate(1_000, {"tx-1": _tx(token="someone-elses")}))

    def test_the_right_token_with_too_little_is_rejected(self):
        self.assertFalse(self._validate(1_000, {"tx-1": _tx(amount_sat=999)}))

    def test_a_transaction_with_no_op_return_at_all_is_rejected(self):
        self.assertFalse(self._validate(1_000, {"tx-1": _tx(token=None)}))

    def test_only_the_outputs_paying_us_count(self):
        # Change back to the payer, and anything paying a third party, are not payments
        # to this node -- exactly as the donation indexer treats them.
        paying_someone_else = _tx(amount_sat=1_000, extra_outputs=[{
            "value": "0.001",
            "scriptPubKey": {"type": "witness_v0_scripthash",
                             "hex": script_pubkey_from_address(OTHER).hex()},
        }])
        self.assertTrue(self._validate(1_000, {"tx-1": paying_someone_else}))
        self.assertFalse(self._validate(2_000, {"tx-1": paying_someone_else}))

    def test_several_outputs_to_us_in_one_transaction_are_summed(self):
        split = _tx(amount_sat=600, extra_outputs=[{
            "value": "0.00000400",
            "scriptPubKey": {"type": "witness_v0_keyhash",
                             "hex": script_pubkey_from_address(ADDRESS).hex()},
        }])
        self.assertTrue(self._validate(1_000, {"tx-1": split}))

    def test_a_payment_to_another_address_is_not_ours(self):
        self.assertFalse(self._validate(1_000, {"tx-1": _tx(paid_to=OTHER)}))

    def test_a_ledger_that_is_not_bitcoin_is_refused(self):
        chain = mock.Mock()
        with mock.patch.object(btc, "backend", return_value=chain):
            self.assertFalse(btc.payment_process_validator(
                amount=1_000, token=TOKEN,
                ledger=celaut_pb2.Contract.Ledger(tags=["ergo"]),
                script=script_pubkey_from_address(ADDRESS),
            ))

    def test_a_script_that_is_not_ours_is_refused(self):
        # The advertised script has to be *this* node's receiving script, or a payment
        # to somebody else's wallet would credit a client here.
        chain = mock.Mock()
        with mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "get_wallet_address", return_value=ADDRESS), \
                mock.patch.object(btc, "NETWORK", lambda: "mainnet"):
            self.assertFalse(btc.payment_process_validator(
                amount=1_000, token=TOKEN, ledger=LEDGER,
                script=script_pubkey_from_address(OTHER),
            ))

    def test_a_transaction_that_cannot_be_read_does_not_answer_no(self):
        """Could not look is not did not pay.

        An unreachable node answering "no" would reject an honest payment with the
        money already on-chain. The other candidates still get their chance.
        """
        from src.payment_system.contracts.bitcoin.backend import BackendUnavailable

        chain = mock.Mock()
        chain.list_received.return_value = [{"txids": ["unreadable", "tx-2"]}]

        def raw(tx_id):
            if tx_id == "unreadable":
                raise BackendUnavailable("node down")
            return _tx()

        chain.raw_transaction.side_effect = raw
        with mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "get_wallet_address", return_value=ADDRESS), \
                mock.patch.object(btc.rate, "mu_per_satoshi", return_value=Decimal(1)), \
                mock.patch.object(btc, "NETWORK", lambda: "mainnet"), \
                mock.patch.object(btc, "MIN_CONFIRMATIONS", lambda: 1):
            self.assertTrue(btc.payment_process_validator(
                amount=1_000, token=TOKEN, ledger=LEDGER,
                script=script_pubkey_from_address(ADDRESS),
            ))


if __name__ == "__main__":
    unittest.main()
