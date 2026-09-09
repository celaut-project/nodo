"""Proving an incoming token payment, against boxes the *payer* built.

The asymmetry that drives every case here: this node builds its own reputation proof
boxes, so the reputation reader may read `assets[0]`. A payment box is built by the
payer, its asset order is the payer's choice, and its ERG value is a carrier the payer
supplied. Reading position zero, or reading `value`, would reject honest payments with
the money already on-chain.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.ergo import interface, rate
    from protos import celaut_pb2
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    interface = None  # type: ignore[assignment]

TOKEN = "ab" * 32
UNRELATED = "cd" * 32
DEPOSIT = "deposit-token-1"
WALLET = "9walletADDR"
SCRIPT = bytes.fromhex("0008cd03" + "77" * 32)


def _asset(decimals=2, mu_per_base=1):
    return rate.Asset(token_id=TOKEN, symbol="SigUSD", unit_name="sigusd",
                      decimals=decimals, mu_per_base_unit=Decimal(mu_per_base))


def _box(r4=DEPOSIT, value=1_000_000, assets=None):
    return {
        "value": value,
        "assets": assets if assets is not None else [],
        "additionalRegisters": {
            "R4": {"renderedValue": r4.encode("utf-8").hex()}
        },
    }


def _token(token_id=TOKEN, amount=100):
    return {"tokenId": token_id, "amount": amount}


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TokenValidationTests(unittest.TestCase):
    def setUp(self):
        self.ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="Ergo", formal=b"")
        # The JVM and the explorer session are not what is under test; the decision is.
        wallet = mock.patch.object(interface, "get_wallet_address", return_value=WALLET)
        wallet.start()
        self.addCleanup(wallet.stop)

        ergo = mock.Mock()
        ergo.get_api_url.return_value = "https://explorer"
        # A module-level name, so no class mangling: the attribute really is called
        # "__init_ergo" and only a string can reach it from inside a class body.
        session = mock.patch.object(interface, "__init_ergo", return_value=ergo)
        session.start()
        self.addCleanup(session.stop)

        derived = mock.Mock()
        derived.toString.return_value = WALLET
        address = mock.patch(
            "src.payment_system.contracts.ergo.ergo_tree.address_from_proposition_bytes",
            return_value=derived,
        )
        address.start()
        self.addCleanup(address.stop)

    def _validate(self, boxes, amount=100, asset=None):
        with mock.patch.object(interface.requests, "get", return_value=_Response(boxes)):
            return interface._validate(
                amount=amount, token=DEPOSIT, ledger=self.ledger, script=SCRIPT,
                asset=asset,
            )

    def test_the_right_token_and_amount_is_accepted(self):
        self.assertTrue(self._validate([_box(assets=[_token(amount=100)])], asset=_asset()))

    def test_more_than_asked_for_is_accepted(self):
        # The payer converts our MU figure from its own scale and rounds down to a whole
        # MU of ours, so a correct payment routinely carries slightly more. Demanding
        # equality rejects it with the money already on-chain.
        self.assertTrue(self._validate([_box(assets=[_token(amount=101)])], asset=_asset()))

    def test_less_than_asked_for_is_rejected(self):
        self.assertFalse(self._validate([_box(assets=[_token(amount=99)])], asset=_asset()))

    def test_the_right_amount_of_the_wrong_token_is_rejected(self):
        # The whole point of identifying an asset by its id: a box holding 100 units of
        # something else is not a payment of 100 SigUSD, however it is named.
        self.assertFalse(
            self._validate([_box(assets=[_token(UNRELATED, 100)])], asset=_asset())
        )

    def test_the_token_behind_an_unrelated_one_is_still_found(self):
        # `assets[0]` is the payer's choice. This is the case that would fail.
        self.assertTrue(self._validate(
            [_box(assets=[_token(UNRELATED, 5), _token(TOKEN, 100)])], asset=_asset()
        ))

    def test_a_box_with_the_right_r4_and_no_assets_is_rejected(self):
        # A carrier box with no token in it is a payment of zero dressed as a payment,
        # and its ERG value must not be read as the amount.
        self.assertFalse(self._validate([_box(value=10**9, assets=[])], asset=_asset()))

    def test_a_token_payment_is_not_measured_in_erg(self):
        # The value of a token box is the carrier the payer supplied, not the payment.
        # Reading it would accept any box whose ERG happened to clear the figure.
        self.assertFalse(self._validate(
            [_box(value=10**9, assets=[_token(amount=1)])], amount=100, asset=_asset()
        ))

    def test_an_erg_payment_still_reads_the_box_value(self):
        # The native path is unchanged: no assets, and `value` is the payment.
        with mock.patch.object(rate, "mu_per_nanoerg", return_value=Decimal(1)):
            self.assertTrue(self._validate([_box(value=100, assets=[])], amount=100))
            self.assertFalse(self._validate([_box(value=99, assets=[])], amount=100))

    def test_a_missing_deposit_token_is_rejected(self):
        self.assertFalse(self._validate(
            [_box(r4="another-deposit", assets=[_token(amount=100)])], asset=_asset()
        ))

    def test_the_amount_is_converted_through_the_assets_own_rate(self):
        # 20 MU per base unit: 100 MU asks for 5 base units, not 100.
        asset = _asset(mu_per_base=20)
        self.assertTrue(self._validate([_box(assets=[_token(amount=5)])], asset=asset))
        self.assertFalse(self._validate([_box(assets=[_token(amount=4)])], asset=asset))

    def test_a_malformed_asset_entry_does_not_crash_the_proof(self):
        # The explorer's payload is not this node's data structure.
        boxes = [_box(assets=[None, {"tokenId": TOKEN, "amount": "not-a-number"},
                              _token(amount=100)])]
        self.assertTrue(self._validate(boxes, asset=_asset()))


if __name__ == "__main__":
    unittest.main()
