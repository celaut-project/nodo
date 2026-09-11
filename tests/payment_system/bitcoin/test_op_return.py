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


def _output(paid_to, amount_sat):
    """One normalised output, as any backend reports one."""
    return {
        "script_hex": script_pubkey_from_address(paid_to).hex(),
        "value_sat": amount_sat,
        "op_return": None,
    }


def _tx(token=TOKEN, paid_to=ADDRESS, amount_sat=1_000, extra_outputs=()):
    """A transaction in the shape the *backend* hands the contract.

    Normalised on purpose: Core reports a value in BTC as a float and an `OP_RETURN` as
    an `asm` string, while an Esplora API reports satoshi integers and a hex script. A
    fixture written in either one's dialect would be testing the contract against a
    backend rather than against the shape it actually reads.
    """
    outputs = []
    if token is not None:
        outputs.append({
            "script_hex": "6a" + f"{len(token):02x}" + token.encode().hex(),
            "value_sat": 0,
            "op_return": token.encode("utf-8"),
        })
    if paid_to is not None:
        outputs.append(_output(paid_to, amount_sat))
    outputs.extend(extra_outputs)
    return {"outputs": outputs}


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
        paying_someone_else = _tx(
            amount_sat=1_000, extra_outputs=[_output(OTHER, 100_000)]
        )
        self.assertTrue(self._validate(1_000, {"tx-1": paying_someone_else}))
        self.assertFalse(self._validate(2_000, {"tx-1": paying_someone_else}))

    def test_several_outputs_to_us_in_one_transaction_are_summed(self):
        split = _tx(amount_sat=600, extra_outputs=[_output(ADDRESS, 400)])
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

    def test_a_read_only_backend_will_not_mint_an_address_even_from_init(self):
        # It cannot ask a node for one, and inventing one would strand payments aimed
        # at whatever peers were already told. With no cold wallet either, there is no
        # address at all, and that is what it says.
        with mock.patch.object(btc, "_signs", return_value=False), \
                mock.patch.object(btc.env_manager, "get", return_value=""):
            with self.assertRaisesRegex(ValueError, "read-only"):
                btc.ensure_receiving_address()

    def test_a_read_only_backend_is_paid_at_the_cold_wallet_and_asks_nobody(self):
        """No key means no hot wallet to be paid into, and no sweep to cold later.

        So the cold wallet is where payers are sent, which leaves the operator one
        address to own rather than a second to configure by hand -- and nothing to ask
        a node for, which is just as well, since this backend has none to ask.
        """
        chain = mock.Mock()
        with mock.patch.object(btc, "_signs", return_value=False), \
                mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "NETWORK", lambda: "mainnet"), \
                mock.patch.object(
                    btc.env_manager, "get",
                    side_effect=lambda key, default=None: (
                        ADDRESS if key == btc.COLD_WALLET_KEY else ""
                    )):
            self.assertEqual(btc.ensure_receiving_address(), ADDRESS)

        self.assertFalse(chain.new_address.called)
        self.assertFalse(chain.receive_address.called)

    def test_the_receiving_path_never_mints_an_address(self):
        """Minting here would check the payment against a script no payer was told.

        `payment_process_validator` runs on the receiving side. An address minted from
        it would be one nobody has been advertised, so a payment that is already
        on-chain would be measured against the wrong script and rejected. `init()` is
        the one place that may mint.
        """
        chain = mock.Mock()
        chain.receive_address.return_value = ""
        with mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "_read_cached_address", return_value=""):
            self.assertFalse(btc.payment_process_validator(
                amount=1_000, token=TOKEN, ledger=LEDGER,
                script=script_pubkey_from_address(ADDRESS),
            ))
        self.assertFalse(chain.new_address.called, "the payment path minted an address")

    def test_init_is_what_mints_the_address(self):
        # A wallet-bearing backend only: a read-only one cannot be asked for an address,
        # which is why it is paid at the cold wallet instead.
        chain = mock.Mock()
        chain.receive_address.return_value = ""
        chain.new_address.return_value = ADDRESS
        with mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "_signs", return_value=True), \
                mock.patch.object(btc, "_read_cached_address", return_value=""), \
                mock.patch.object(btc, "_remember_address") as remembered, \
                mock.patch.object(btc, "NETWORK", lambda: "mainnet"), \
                mock.patch.object(btc.sql_connection, "SQLConnection") as sql:
            btc.init()

        remembered.assert_called_once_with(ADDRESS)
        # Advertised as the raw scriptPubKey, never as a readable address.
        contract = sql.return_value.add_contract.call_args.kwargs["contract"]
        from src.utils.contract_xattrs import get_script, get_token_id
        self.assertEqual(get_script(contract), script_pubkey_from_address(ADDRESS))
        self.assertEqual(get_token_id(contract), "BTC")

    def test_a_transaction_reorged_out_between_the_two_checks_is_rejected(self):
        """The payer counted confirmations; by the time we look, they are gone.

        `listreceivedbyaddress` is asked for transactions with at least
        MIN_CONFIRMATIONS, so one that left the chain is simply not there -- and the
        answer is no rather than an exception, which is what makes the orchestrator
        record it as a refused deposit instead of crediting a client for nothing.
        """
        chain = mock.Mock()
        chain.list_received.return_value = [{"txids": []}]
        with mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "get_wallet_address", return_value=ADDRESS), \
                mock.patch.object(btc.rate, "mu_per_satoshi", return_value=Decimal(1)), \
                mock.patch.object(btc, "NETWORK", lambda: "mainnet"), \
                mock.patch.object(btc, "MIN_CONFIRMATIONS", lambda: 1):
            self.assertFalse(btc.payment_process_validator(
                amount=1_000, token=TOKEN, ledger=LEDGER,
                script=script_pubkey_from_address(ADDRESS),
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
