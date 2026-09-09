"""What the payee's box carries, and what comes back as change.

An input box holding SigUSD and SigRSV pays SigUSD out of it. Two failures would be
invisible until an operator noticed funds missing: sending the payee the whole box
(giving away an asset nobody asked for, and paying for the transfer), or building a
transaction that does not account for the second asset at all.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.payment_system.contracts.ergo import interface, rate
    from src.utils.contract_xattrs import get_token_id
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    interface = None  # type: ignore[assignment]

TOKEN = "ab" * 32
OTHER = "cd" * 32
DEPOSIT = "deposit-token-1"
SCRIPT = bytes.fromhex("0008cd03" + "77" * 32)


def _asset(mu_per_base=1):
    return rate.Asset(token_id=TOKEN, symbol="SigUSD", unit_name="sigusd",
                      decimals=2, mu_per_base_unit=Decimal(mu_per_base))


class _ErgoToken:
    def __init__(self, token_id, amount):
        self.id, self.amount = str(token_id), int(amount)


class _OutBoxBuilder:
    """Records what the payee's box is built with."""

    def __init__(self, record):
        self.record = record

    def value(self, value):
        self.record["value"] = int(value)
        return self

    def tokens(self, tokens):
        self.record["tokens"] = [(t.id, t.amount) for t in tokens]
        return self

    def registers(self, registers):
        self.record["registers"] = list(registers)
        return self

    def contract(self, contract):
        self.record["contract"] = contract
        return self

    def build(self):
        return self.record


class _FakeErgo:
    def __init__(self, record):
        self.record = record
        self._ctx = mock.Mock()
        self._ctx.newTxBuilder.return_value.outBoxBuilder.side_effect = (
            lambda: _OutBoxBuilder(record)
        )

    def get_api_url(self):
        return "https://explorer"

    def getInputBoxCovering(self, amount_list, sender_address, tokenList=None,
                            amount_tokens=None):
        self.record["covering"] = {
            "amount_list": list(amount_list), "tokenList": tokenList,
            "amount_tokens": amount_tokens,
        }
        # A wallet box that also holds an unrelated asset. What happens to it is the
        # point of this file.
        return ["input-box-with-sigusd-and-sigrsv"]

    def buildUnsignedTransaction(self, input_box, outBox, fee, sender_address):
        self.record["built"] = {"input_box": input_box, "outBox": outBox, "fee": fee,
                                "change_to": sender_address}
        return "unsigned"

    def getMnemonic(self, wallet_mnemonic, mnemonic_password=None):
        return ("mnemonic", "seed", "password")

    def signTransaction(self, unsigned_tx, mnemonic, prover_index=0):
        return "signed"

    def txId(self, signed_tx):
        return "tx-id-1"


class _Confirmed:
    status_code = 200

    @staticmethod
    def json():
        return {"numConfirmations": 2}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TokenOutBoxTests(unittest.TestCase):
    def setUp(self):
        self.record = {}
        self.ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo", formal=b"")
        self.sender = mock.Mock(name="sender-address")

        jpype = mock.Mock()
        jpype.JLong.side_effect = int
        jpype.JString.return_value.getBytes.return_value = b"deposit"
        org_appkit = mock.Mock()
        org_appkit.ErgoToken.side_effect = _ErgoToken

        for name, patched in (
            ("_ergo_runtime", mock.patch.object(
                interface, "_ergo_runtime",
                return_value=(mock.Mock(), mock.Mock(), jpype, org_appkit))),
            ("__init_ergo", mock.patch.object(
                interface, "__init_ergo", return_value=_FakeErgo(self.record))),
            ("__get_sender_addr", mock.patch.object(
                interface, "__get_sender_addr", return_value=self.sender)),
            ("contract", mock.patch(
                "src.payment_system.contracts.ergo.interface."
                "ergo_contract_from_proposition_bytes",
                return_value="payee-contract")),
            ("sleep", mock.patch.object(interface, "sleep", lambda _seconds: None)),
            ("requests", mock.patch.object(interface.requests, "get",
                                           return_value=_Confirmed())),
            ("mnemonic", mock.patch.object(interface, "WALLET_MNEMONIC",
                                           lambda: "words")),
        ):
            patched.start()
            self.addCleanup(patched.stop)

    def _pay(self, amount=100, asset=None):
        return interface._settle(amount=amount, deposit_token=DEPOSIT,
                                 ledger=self.ledger, script=SCRIPT, asset=asset)

    def test_the_payee_gets_the_paid_token_and_nothing_else(self):
        self._pay(asset=_asset())
        # Exactly one token, the one that was paid. The unrelated asset the input box
        # also held is not in the payee's box: forwarding it would give away money
        # nobody asked for, and pay for the transfer.
        self.assertEqual(self.record["tokens"], [(TOKEN, 100)])

    def test_the_box_value_is_the_carrier_not_the_payment(self):
        self._pay(asset=_asset())
        # ERG this node supplies so the token has a box to travel in -- the technical
        # minimum, not the amount and not whatever the input box held.
        self.assertEqual(self.record["value"], interface.SAFE_MIN_BOX_VALUE)

    def test_change_goes_back_to_this_wallet(self):
        # Which is where the unrelated asset ends up: AppKit returns every input token
        # not spent by an output to the change address. Change anywhere else would lose
        # it.
        self._pay(asset=_asset())
        self.assertIs(self.record["built"]["change_to"], self.sender)

    def test_the_inputs_are_asked_to_cover_the_token_as_well_as_the_fee(self):
        self._pay(asset=_asset())
        covering = self.record["covering"]
        self.assertEqual(covering["tokenList"], [[TOKEN]])
        self.assertEqual(covering["amount_tokens"], [[100]])
        # ergpy reads `amount_list` in whole ERG: the carrier box plus the fee.
        self.assertEqual(
            covering["amount_list"],
            [(interface.SAFE_MIN_BOX_VALUE + interface.DEFAULT_FEE) / 10**9],
        )

    def test_the_receipt_names_the_asset_that_was_paid(self):
        contract = self._pay(asset=_asset())
        # Which is what lets the peer file the credit against the method it advertised
        # rather than against this contract's native unit.
        self.assertEqual(get_token_id(contract), TOKEN)

    def test_the_amount_is_converted_through_the_assets_own_rate(self):
        self._pay(amount=100, asset=_asset(mu_per_base=20))
        self.assertEqual(self.record["tokens"], [(TOKEN, 5)])

    def test_less_than_one_base_unit_cannot_be_settled(self):
        # A box with an empty token list is a payment of zero dressed as a payment.
        with self.assertRaisesRegex(Exception, "one base unit"):
            self._pay(amount=19, asset=_asset(mu_per_base=20))

    def test_an_erg_payment_carries_no_token_and_the_value_is_the_payment(self):
        with mock.patch.object(rate, "mu_per_nanoerg", return_value=Decimal(1)):
            contract = self._pay(amount=2_000_000)
        self.assertEqual(self.record["value"], 2_000_000)
        self.assertNotIn("tokens", self.record)
        self.assertEqual(get_token_id(contract), "ERG")
        # And it is not asked to cover a token it is not paying in.
        self.assertIsNone(self.record["covering"]["tokenList"])

    def test_the_deposit_token_is_in_r4_either_way(self):
        # It is what links the payment back to the client who made it, so a token
        # payment that dropped it would arrive unattributable.
        self._pay(asset=_asset())
        self.assertEqual(len(self.record["registers"]), 1)


if __name__ == "__main__":
    unittest.main()
