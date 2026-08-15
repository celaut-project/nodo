"""`nodo tx_history` has to say who was on the other side.

It printed an id, an amount, a timestamp and a direction -- everything except the
one thing a payment is about. The counterparty address was in the explorer response
all along and was dropped; the *identity* behind it was never on chain to begin with,
so it comes from what this node recorded when it paid, or from the deposit token an
incoming box carries in R4 (the same register the node validates payments with).

The fallbacks are the point of these tests: a wallet has activity nodo did not make,
and a node upgraded mid-life has transactions older than its payments table. Neither
may print less than the raw address, and neither may raise.
"""
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import src.commands.tx_history as tx_history

OURS = "9ourWALLETaddress"
THEIRS = "9theirCONTRACTaddress"


def _tx(tx_id="tx-1", inputs=(), outputs=()):
    return {
        "id": tx_id,
        "timestamp": 1_700_000_000_000,
        "numConfirmations": 12,
        "inputs": list(inputs),
        "outputs": list(outputs),
    }


def _box(address, value=1_000_000, r4_token=None):
    box = {"address": address, "value": value}
    if r4_token is not None:
        box["additionalRegisters"] = {
            "R4": {"renderedValue": r4_token.encode("utf-8").hex()}
        }
    return box


class CounterpartyLineTests(unittest.TestCase):

    def test_an_outgoing_payment_names_the_peer_it_was_recorded_against(self):
        tx = _tx(outputs=[_box(THEIRS), _box(OURS, value=50)])  # second output is change
        lines = tx_history._counterparty_lines(
            tx, OURS, "Outgoing",
            payments={"tx-1": {"peer_id": "peer-1", "status": "communicated"}},
            clients_by_token={},
        )

        self.assertIn("To: peer peer-1", lines)
        self.assertIn(f"To address: {THEIRS}", lines)
        # Change coming back to us is not a counterparty.
        self.assertNotIn(f"To address: {OURS}", lines)

    def test_a_payment_the_peer_never_acknowledged_says_so(self):
        tx = _tx(outputs=[_box(THEIRS)])
        lines = tx_history._counterparty_lines(
            tx, OURS, "Outgoing",
            payments={"tx-1": {"peer_id": "peer-1", "status": "unacknowledged"}},
            clients_by_token={},
        )

        self.assertTrue(any("never acknowledged" in line for line in lines))

    def test_a_recorded_payment_settles_the_direction_the_explorer_could_not(self):
        """Inputs without addresses make the direction "Unknown"; our own row does not."""
        tx = _tx(inputs=[{"value": 2_000_000}], outputs=[_box(THEIRS)])
        lines = tx_history._counterparty_lines(
            tx, OURS, "Unknown",
            payments={"tx-1": {"peer_id": "peer-1", "direction": "out",
                               "status": "communicated"}},
            clients_by_token={},
        )

        self.assertIn("To: peer peer-1", lines)
        self.assertIn(f"To address: {THEIRS}", lines)

    def test_an_incoming_payment_is_named_by_the_deposit_token_it_carries(self):
        tx = _tx(inputs=[_box("9payerADDRESS")],
                 outputs=[_box(OURS, r4_token="deposit-token-1")])
        lines = tx_history._counterparty_lines(
            tx, OURS, "Incoming",
            payments={},
            clients_by_token={"deposit-token-1": "client-1"},
        )

        self.assertTrue(any("From: client client-1" in line for line in lines))
        self.assertIn("From address: 9payerADDRESS", lines)

    def test_an_unknown_transaction_still_shows_the_raw_address(self):
        tx = _tx(tx_id="tx-unknown", outputs=[_box(THEIRS)])
        lines = tx_history._counterparty_lines(
            tx, OURS, "Outgoing", payments={}, clients_by_token={}
        )

        self.assertEqual(lines, [f"To address: {THEIRS}"])

    def test_a_register_that_is_not_a_deposit_token_is_ignored(self):
        """R4 belongs to whatever application wrote it; ours is UTF-8, others are not."""
        tx = _tx(inputs=[_box("9payerADDRESS")], outputs=[_box(OURS)])
        tx["outputs"][0]["additionalRegisters"] = {"R4": {"renderedValue": "ffff"}}

        lines = tx_history._counterparty_lines(
            tx, OURS, "Incoming", payments={}, clients_by_token={}
        )

        self.assertEqual(lines, ["From address: 9payerADDRESS"])


class DisplayWiringTests(unittest.TestCase):
    """The lookups have to reach the printer, which is where wiring like this dies."""

    def test_the_peer_is_printed_under_the_transaction(self):
        tx = _tx(inputs=[_box(OURS, value=2_000_000)], outputs=[_box(THEIRS)])

        with mock.patch.object(tx_history, "_get_address_transactions", return_value=[tx]), \
                mock.patch.object(tx_history, "_payments_by_tx_id",
                                  return_value={"tx-1": {"peer_id": "peer-1",
                                                         "status": "communicated"}}), \
                mock.patch.object(tx_history, "_clients_by_deposit_token", return_value={}), \
                mock.patch("src.payment_system.contracts.ergo.interface.__nanoerg_to_erg",
                           create=True, side_effect=lambda value: value / 10 ** 9):
            output = io.StringIO()
            with redirect_stdout(output):
                tx_history._display_wallet_transactions("Wallet", OURS)

        printed = output.getvalue()
        self.assertIn("Transaction ID: tx-1", printed)
        self.assertIn("To: peer peer-1", printed)
        self.assertIn(f"To address: {THEIRS}", printed)


if __name__ == "__main__":
    unittest.main()
