"""What `nodo tx_history` shows for a transaction that moved a token.

Reported as one row per transaction, a token payment shows 0.001 ERG -- the box the
token travelled in -- and the money it actually moved is invisible. So one row per asset
a transaction moved, sharing its id, each an amount in its own money. The ERG row of a
token payment is worth seeing for what it is: the carrier and the fee are the ERG cost
of moving a token.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.ergo import history, rate
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    history = None  # type: ignore[assignment]

WALLET = "9walletADDR"
PEER = "9peerADDR"
TOKEN = "ab" * 32
OTHER = "cd" * 32


def _box(address, value, assets=None):
    return {"address": address, "value": value, "assets": assets or []}


def _token(token_id=TOKEN, amount=100):
    return {"tokenId": token_id, "amount": amount}


def _transaction(inputs, outputs, tx_id="tx-1"):
    return {
        "id": tx_id, "timestamp": 1_760_000_000_000, "numConfirmations": 3,
        "inputs": inputs, "outputs": outputs,
    }


class _Response:
    status_code = 200

    def __init__(self, items):
        self._items = items

    def json(self):
        return {"items": self._items}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TokenHistoryTests(unittest.TestCase):
    def setUp(self):
        asset = rate.Asset(token_id=TOKEN, symbol="SigUSD", unit_name="sigusd",
                           decimals=2, mu_per_base_unit=Decimal(1))
        patcher = mock.patch.object(rate, "assets", return_value=(asset,))
        patcher.start()
        self.addCleanup(patcher.stop)
        explorer = mock.patch(
            "src.payment_system.contracts.ergo.history.explorer_api_url",
            return_value="https://explorer",
        )
        explorer.start()
        self.addCleanup(explorer.stop)

    def _rows(self, *transactions):
        with mock.patch.object(history.requests, "get",
                               return_value=_Response(list(transactions))):
            return history.transaction_history(WALLET)

    def test_an_erg_only_transaction_is_one_row_as_before(self):
        # A node that knows nothing about tokens sees exactly what it always saw.
        rows = self._rows(_transaction([_box(PEER, 2_000_000)],
                                       [_box(WALLET, 2_000_000)]))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["unit"], rows[0]["amount"], rows[0]["direction"]),
                         ("ERG", 2_000_000, "in"))

    def test_a_token_payment_reports_the_token_and_its_carrier(self):
        rows = self._rows(_transaction(
            [_box(PEER, 3_000_000, [_token(amount=100)])],
            [_box(WALLET, 1_000_000, [_token(amount=100)])],
        ))
        self.assertEqual(
            [(row["unit"], row["amount"], row["direction"]) for row in rows],
            [("ERG", 1_000_000, "in"), ("SigUSD", 100, "in")],
        )
        # Same transaction, so the rows share its id and its confirmations.
        self.assertEqual({row["id"] for row in rows}, {"tx-1"})
        self.assertEqual({row["confirmations"] for row in rows}, {3})

    def test_the_amount_is_rendered_at_the_configured_decimals(self):
        rows = self._rows(_transaction(
            [_box(PEER, 2_000_000, [_token(amount=1234)])],
            [_box(WALLET, 1_000_000, [_token(amount=1234)])],
        ))
        self.assertEqual(rows[1]["decimals"], 2)

    def test_only_the_net_movement_of_each_token_is_reported(self):
        # Change comes back to the sender, so a payment of 40 out of a box of 100 is a
        # movement of 40 -- not 100 out and 60 in.
        rows = self._rows(_transaction(
            [_box(WALLET, 5_000_000, [_token(amount=100)])],
            [_box(PEER, 1_000_000, [_token(amount=40)]),
             _box(WALLET, 3_000_000, [_token(amount=60)])],
        ))
        token_rows = [row for row in rows if row["unit"] == "SigUSD"]
        self.assertEqual([(r["amount"], r["direction"]) for r in token_rows],
                         [(40, "out")])

    def test_a_token_that_did_not_move_is_not_reported(self):
        # It was in the inputs and came back in the change: nothing happened to it, and
        # a row saying otherwise would read as a payment.
        rows = self._rows(_transaction(
            [_box(WALLET, 5_000_000, [_token(amount=100)])],
            [_box(PEER, 1_000_000), _box(WALLET, 3_000_000, [_token(amount=100)])],
        ))
        self.assertEqual([row["unit"] for row in rows], ["ERG"])

    def test_several_assets_in_one_transaction_are_several_rows(self):
        rows = self._rows(_transaction(
            [_box(PEER, 3_000_000, [_token(TOKEN, 100), _token(OTHER, 7)])],
            [_box(WALLET, 1_000_000, [_token(TOKEN, 100), _token(OTHER, 7)])],
        ))
        self.assertEqual([row["unit"] for row in rows], ["ERG", "SigUSD", "cdcdcdcdcdcd…"])
        # An unconfigured token has no declared decimals, so its base units are shown
        # as they are rather than scaled by a number nobody stated.
        self.assertEqual(rows[2]["decimals"], 0)

    def test_a_malformed_assets_list_does_not_break_the_read(self):
        rows = self._rows(_transaction(
            [_box(PEER, 3_000_000, [None, {"tokenId": TOKEN, "amount": "x"},
                                    _token(amount=5)])],
            [_box(WALLET, 1_000_000, [_token(amount=5)])],
        ))
        self.assertEqual([row["unit"] for row in rows], ["ERG", "SigUSD"])

    def test_a_broken_assets_config_still_reports_the_movement(self):
        # A malformed ASSETS list is a startup error; a history read is not where an
        # operator should meet it, and the movement is real either way.
        with mock.patch.object(rate, "assets", side_effect=ValueError("bad id")):
            rows = self._rows(_transaction(
                [_box(PEER, 3_000_000, [_token(amount=5)])],
                [_box(WALLET, 1_000_000, [_token(amount=5)])],
            ))
        self.assertEqual([row["unit"] for row in rows], ["ERG", "abababababab…"])


if __name__ == "__main__":
    unittest.main()
