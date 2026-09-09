"""Paying the accrued donation, on the tick that already manages the contract.

What is pinned here is the arithmetic around the transaction, not the transaction:

* The threshold is in native units. Read out of ``settlement_floors_mu()`` -- which
  reports MU -- it would be wrong by exactly ``MU_PER_NANOERG``: invisible at the
  default of 1, silently wrong for anyone who changes it.
* The debt is decremented once, after the transaction is on the wire, by exactly what
  the transaction discharged.
* ``SIMULATE_PAYMENTS`` accrues and logs but broadcasts nothing, and leaves the debt.
* One tick produces one donation transaction, and pays the debt before sweeping the
  wallet's excess to cold storage -- the debt is owed, the sweep is discretionary.
"""
import sys
import types
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.ergo import interface
    from src.payment_system.donations.config import Wallet
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    interface = None  # type: ignore[assignment]

WALLET_A = "9gGZp7HRAFxgGWSwvS4hCbxM2RpkYr6pHvwpU4GPrpvxY7Y2nQo"
WALLET_B = "9hHDQb26AjnJUXxcqriqY1mnhpLuUeC81C4pggtK7tupr92Ea1K"


class _Catalogue:
    """The donation rows, as the payout reads and writes them."""

    def __init__(self, owed):
        self.owed = Decimal(str(owed))
        self.settlements = []

    def donation_owed(self, ledger, contract_hash, token_id):
        return self.owed

    def settle_donation(self, *, ledger, contract_hash, token_id, paid_native, records):
        self.settlements.append({
            "ledger": ledger,
            "token_id": token_id,
            "paid_native": paid_native,
            "records": records,
        })
        self.owed -= Decimal(str(paid_native))
        return True


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PayoutTests(unittest.TestCase):

    def _pay(self, owed, *, wallets=None, simulate=False, min_transfer=2_000_000,
             tx_id="tx-donation", balance=10 ** 12):
        catalogue = _Catalogue(owed)
        sent = []

        def simple_send(ergo, amount, receiver_addresses, wallet_mnemonic, fee):
            sent.append({
                "amount": amount,
                "receivers": receiver_addresses,
                "fee": fee,
            })
            return tx_id

        stub = types.ModuleType("src.database.sql_connection")
        stub.SQLConnection = lambda: catalogue  # type: ignore[attr-defined]

        with mock.patch.dict(sys.modules, {"src.database.sql_connection": stub}), \
                mock.patch.object(interface.sql_connection, "SQLConnection",
                                  lambda: catalogue, create=True), \
                mock.patch.object(interface, "_donation_min_transfer_nanoerg",
                                  return_value=min_transfer), \
                mock.patch.object(interface, "SIMULATE_PAYMENTS", lambda: simulate), \
                mock.patch.object(interface, "WALLET_MNEMONIC", lambda: "mnemonic"), \
                mock.patch.object(interface, "_ergo_runtime",
                                  return_value=(None, simple_send, None, None)), \
                mock.patch.object(interface, "__init_ergo", lambda: object(), create=True), \
                mock.patch.object(interface, "__get_sender_addr",
                                  lambda mnemonic: object(), create=True), \
                mock.patch.object(interface, "__confirmed_balance_nanoerg",
                                  lambda address: balance, create=True), \
                mock.patch(
                    "src.payment_system.donations.config.pay_wallets",
                    return_value=wallets if wallets is not None
                    else [Wallet(WALLET_A, Decimal(1))],
                ):
            interface._pay_accrued_donations()
        return catalogue, sent

    def test_a_debt_worth_a_transaction_is_paid_and_decremented_once(self):
        catalogue, sent = self._pay(11_000_000)

        self.assertEqual(len(sent), 1)
        # simple_send takes whole ERG; 1e7 nanoERG of donation and 1e6 of fee.
        self.assertEqual(sent[0]["receivers"], [WALLET_A])
        self.assertAlmostEqual(sent[0]["amount"][0], 0.01)
        self.assertAlmostEqual(sent[0]["fee"], 0.001)

        self.assertEqual(len(catalogue.settlements), 1)
        # The fee comes out of the debt, so what is discharged is output plus fee.
        self.assertEqual(catalogue.settlements[0]["paid_native"], 11_000_000)
        self.assertEqual(catalogue.owed, 0)

    def test_the_payment_row_names_the_transaction_and_where_it_went(self):
        catalogue, _ = self._pay(11_000_000)
        record = catalogue.settlements[0]["records"][0]

        self.assertEqual(record["tx_id"], "tx-donation")
        self.assertEqual(record["address"], WALLET_A)
        # MU for the ledger-neutral column, at this ledger's rate.
        self.assertEqual(record["amount_mu"], 10_000_000)

    def test_weights_split_the_payout_within_one_transaction(self):
        _, sent = self._pay(
            11_000_000,
            wallets=[Wallet(WALLET_A, Decimal("0.7")), Wallet(WALLET_B, Decimal("0.3"))],
            min_transfer=3_000_000,
        )
        self.assertEqual(len(sent), 1, "one tick, one donation transaction")
        self.assertEqual(sent[0]["receivers"], [WALLET_A, WALLET_B])
        self.assertAlmostEqual(sent[0]["amount"][0], 0.007)
        self.assertAlmostEqual(sent[0]["amount"][1], 0.003)

    def test_a_debt_below_the_minimum_transfer_waits(self):
        catalogue, sent = self._pay(5_000_000, min_transfer=100_000_000)

        self.assertEqual(sent, [])
        self.assertEqual(catalogue.settlements, [])
        self.assertEqual(catalogue.owed, 5_000_000)

    def test_the_threshold_does_not_move_with_the_mu_rate(self):
        """The regression guard for the MU/native mix-up.

        The debt and the chain's floors are both native, so the same debt clears the
        same threshold at any rate. An implementation reading the floors in MU would
        pay a debt ten times too small -- or refuse one ten times too large -- the
        moment an operator set ``MU_PER_NANOERG`` to anything but 1.
        """
        from src.payment_system.contracts.ergo import rate

        with mock.patch.object(rate, "mu_per_nanoerg", return_value=Decimal(10)):
            catalogue, sent = self._pay(11_000_000)
        self.assertEqual(len(sent), 1)
        self.assertEqual(catalogue.settlements[0]["paid_native"], 11_000_000)

        with mock.patch.object(rate, "mu_per_nanoerg", return_value=Decimal(10)):
            _, nothing = self._pay(1_500_000)
        self.assertEqual(nothing, [])

    def test_simulated_payments_broadcast_nothing_and_keep_the_debt(self):
        catalogue, sent = self._pay(11_000_000, simulate=True)

        self.assertEqual(sent, [])
        self.assertEqual(catalogue.settlements, [])
        self.assertEqual(catalogue.owed, 11_000_000)

    def test_a_share_below_ergos_minimum_output_stays_accrued(self):
        catalogue, sent = self._pay(
            11_000_000,
            wallets=[Wallet(WALLET_A, Decimal("0.9999")), Wallet(WALLET_B, Decimal("0.0001"))],
        )
        self.assertEqual(sent[0]["receivers"], [WALLET_A])
        # Not redistributed to the wallet that could be paid: it is still owed, so the
        # small weight is paid on a later tick instead of being ignored for ever.
        self.assertGreater(catalogue.owed, 0)

    def test_an_invalid_address_is_skipped_and_does_not_block_the_others(self):
        catalogue, sent = self._pay(
            11_000_000,
            wallets=[Wallet("not-an-address", Decimal("0.5")), Wallet(WALLET_A, Decimal("0.5"))],
        )
        self.assertEqual(sent[0]["receivers"], [WALLET_A])
        self.assertGreater(catalogue.owed, 0, "the skipped wallet's share is not given away")

    def test_no_valid_wallet_means_nothing_is_sent_and_nothing_is_lost(self):
        catalogue, sent = self._pay(
            11_000_000, wallets=[Wallet("not-an-address", Decimal(1))]
        )
        self.assertEqual(sent, [])
        self.assertEqual(catalogue.owed, 11_000_000)

    def test_a_wallet_that_cannot_cover_the_payout_keeps_the_debt(self):
        """The debt came out of money that arrived, but the wallet may have spent it.

        Peer deposits and manual transfers come out of the same wallet, so the funds
        can be gone by the time the tick fires. Checked before broadcasting so the log
        names the reason and the debt is visibly kept rather than looking lost.
        """
        catalogue, sent = self._pay(11_000_000, balance=5_000_000)

        self.assertEqual(sent, [])
        self.assertEqual(catalogue.settlements, [])
        self.assertEqual(catalogue.owed, 11_000_000)

    def test_nothing_owed_does_nothing(self):
        catalogue, sent = self._pay(0)
        self.assertEqual(sent, [])
        self.assertEqual(catalogue.settlements, [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TickOrderTests(unittest.TestCase):

    def test_one_tick_pays_the_debt_before_sweeping_the_excess(self):
        """The debt is owed; the sweep is discretionary.

        Swept first, the excess would leave for cold storage and the donation would
        wait another whole interval for funds that were there.
        """
        order = []
        with mock.patch.object(interface, "_pay_accrued_donations",
                               side_effect=lambda: order.append("donate")), \
                mock.patch.object(interface, "_sweep_to_cold_wallet",
                                  side_effect=lambda: order.append("sweep")):
            interface.manager()
        self.assertEqual(order, ["donate", "sweep"])

    def test_the_sweep_no_longer_splits_off_a_donation(self):
        """Donating has nothing to do with the cold wallet any more.

        It used to be a cut of the sweep, which meant a node with no cold wallet -- the
        default -- donated nothing whatever its percentage said, and a node earning
        less than its hot-wallet limit donated nothing either.
        """
        import inspect

        source = inspect.getsource(interface._sweep_to_cold_wallet)
        self.assertNotIn("DONATION", source)
        self.assertFalse(hasattr(interface, "ERGO_DONATION_WALLET"))


if __name__ == "__main__":
    unittest.main()
