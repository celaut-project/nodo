"""`nodo tx_history` has to say who was on the other side.

It printed an id, an amount, a timestamp and a direction -- everything except the
one thing a payment is about. The counterparty address was in the explorer response
all along and was dropped; the *identity* behind it was never on chain to begin with,
so it comes from what this node recorded when it paid, or from the deposit token the
transaction carries -- Ergo's in register R4, Bitcoin's in an `OP_RETURN`, both reported
the same way by their contract.

The fallbacks are the point of these tests: a wallet has activity nodo did not make,
and a node upgraded mid-life has transactions older than its payments table. Neither
may print less than the raw address, and neither may raise.
"""
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import src.commands.tx_history as tx_history

# Every test here is now pure: the rows are the normalised shape each contract
# answers in, so naming a counterparty needs no chain, no explorer and no JVM. Walking
# Ergo boxes to produce those rows is Ergo's own business and is tested with it.

OURS = "9ourWALLETaddress"
THEIRS = "9theirCONTRACTaddress"


def _row(tx_id="tx-1", direction="out", counterparties=(THEIRS,), deposit_tokens=(),
         amount=1_000_000):
    """One transaction as a payment contract reports it, chain-shape already resolved."""
    return {
        "id": tx_id,
        "timestamp": 1_700_000_000,
        "confirmations": 12,
        "direction": direction,
        "amount": amount,
        "unit": "ERG",
        "decimals": 9,
        "counterparties": list(counterparties),
        "deposit_tokens": list(deposit_tokens),
    }


class CounterpartyLineTests(unittest.TestCase):

    def test_an_outgoing_payment_names_the_peer_it_was_recorded_against(self):
        lines = tx_history._counterparty_lines(
            _row(),
            payments={"tx-1": {"peer_id": "peer-1", "status": "communicated"}},
            clients_by_token={},
        )

        self.assertIn("To: peer peer-1", lines)
        self.assertIn(f"To address: {THEIRS}", lines)

    def test_a_donation_says_what_it_was_for(self):
        # `purpose` is orthogonal to `status`, so a donation reads as a donation rather
        # than as a payment to a peer nobody can name.
        lines = tx_history._counterparty_lines(
            _row(counterparties=("9devWALLET",)),
            payments={"tx-1": {"status": "accepted", "purpose": "donation"}},
            clients_by_token={},
        )
        self.assertIn("Purpose: donation", lines)

    def test_a_payment_the_peer_never_acknowledged_says_so(self):
        lines = tx_history._counterparty_lines(
            _row(),
            payments={"tx-1": {"peer_id": "peer-1", "status": "unacknowledged"}},
            clients_by_token={},
        )

        self.assertTrue(any("never got an acknowledgement" in line for line in lines))

    def test_a_recorded_payment_settles_the_direction_the_chain_could_not(self):
        """A chain that reports inputs without addresses cannot say which side we are on.

        Our own row can: it exists because this node signed the transaction.
        """
        lines = tx_history._counterparty_lines(
            _row(direction="unknown"),
            payments={"tx-1": {"peer_id": "peer-1", "direction": "out",
                               "status": "communicated"}},
            clients_by_token={},
        )

        self.assertIn("To: peer peer-1", lines)
        self.assertIn(f"To address: {THEIRS}", lines)

    def test_an_incoming_payment_is_named_by_the_deposit_token_it_carries(self):
        lines = tx_history._counterparty_lines(
            _row(direction="in", counterparties=("9payerADDRESS",),
                 deposit_tokens=("deposit-token-1",)),
            payments={},
            clients_by_token={"deposit-token-1": "client-1"},
        )

        self.assertTrue(any("From: client client-1" in line for line in lines))
        self.assertIn("From address: 9payerADDRESS", lines)

    def test_a_deposit_token_this_node_never_issued_is_still_reported(self):
        lines = tx_history._counterparty_lines(
            _row(direction="in", counterparties=("9payerADDRESS",),
                 deposit_tokens=("someone-elses",)),
            payments={}, clients_by_token={},
        )
        self.assertTrue(any("unknown deposit token" in line for line in lines))

    def test_an_unknown_transaction_still_shows_the_raw_address(self):
        # A wallet has activity nodo did not make, and a node upgraded mid-life has
        # transactions older than its payments table. Neither may print less than the
        # address, and neither may raise.
        lines = tx_history._counterparty_lines(
            _row(tx_id="tx-unknown"), payments={}, clients_by_token={}
        )

        self.assertEqual(lines, [f"To address: {THEIRS}"])

    def test_a_transaction_with_nobody_on_the_other_side_says_so(self):
        lines = tx_history._counterparty_lines(
            _row(counterparties=()), payments={}, clients_by_token={}
        )
        self.assertEqual(lines, ["Counterparty: unknown"])


class DisplayWiringTests(unittest.TestCase):
    """The lookups have to reach the printer, which is where wiring like this dies."""

    def test_the_peer_is_printed_under_the_transaction(self):
        contract = mock.Mock()
        contract.LEDGER = "ergo"
        contract.get_wallet_address.return_value = OURS
        contract.transaction_history.return_value = [_row(amount=2_000_000)]

        with mock.patch.object(tx_history, "_payments_by_tx_id",
                               return_value={"tx-1": {"peer_id": "peer-1",
                                                      "status": "communicated"}}):
            output = io.StringIO()
            with redirect_stdout(output):
                tx_history._display_contract_history(contract, {}, 10)

        printed = output.getvalue()
        self.assertIn("Transaction ID: tx-1", printed)
        self.assertIn("To: peer peer-1", printed)
        self.assertIn(f"To address: {THEIRS}", printed)
        # The amount in the chain's own money, with its own decimals.
        self.assertIn("0.002000000 ERG", printed)


if __name__ == "__main__":
    unittest.main()
