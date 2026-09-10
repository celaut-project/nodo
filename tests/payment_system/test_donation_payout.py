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
    """The donation rows, as the payout reads and writes them.

    Two rows, not one: the debt, and what each wallet has already been credited
    against its share. The second is what makes a weight mean a share of everything
    this method ever earned rather than a share of one transaction -- see
    `tests/payment_system/test_donation_split.py::SequenceTests`.
    """

    def __init__(self, owed, paid=None):
        self.owed = Decimal(str(owed))
        self.paid = {a: Decimal(str(v)) for a, v in (paid or {}).items()}
        self.settlements = []

    def donation_owed(self, ledger, contract_hash, token_id):
        return self.owed

    def donation_paid_by_address(self, ledger, contract_hash, token_id):
        return dict(self.paid)

    def settle_donation(self, *, ledger, contract_hash, token_id, paid_native, records,
                        credited=(), paid_before=None):
        self.settlements.append({
            "ledger": ledger,
            "token_id": token_id,
            "paid_native": paid_native,
            "records": records,
            "credited": list(credited),
        })
        self.owed -= Decimal(str(paid_native))
        # Against the map the payout planned with, exactly as the real one does: the
        # credit is written after the transaction is on the wire, so re-reading here is
        # a read whose failure could abort nothing.
        base = paid_before if paid_before is not None else {}
        for address, amount in credited:
            self.paid[address] = (
                Decimal(str(base.get(address, 0))) + Decimal(str(amount))
            )
        return True


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PayoutTests(unittest.TestCase):

    def _pay(self, owed, *, wallets=None, simulate=False, min_transfer=2_000_000,
             tx_id="tx-donation", balance=10 ** 12, paid=None, catalogue=None):
        catalogue = catalogue if catalogue is not None else _Catalogue(owed, paid)
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

    def test_each_wallet_is_credited_what_the_payout_discharged_of_its_share(self):
        """The row the next payout reads. Written in the same commit as the decrement.

        Without it the next tick recomputes every cut from the live debt, and a cut too
        small to go out is shared among everybody -- so the wallet that earned it never
        gets it. The credit is the wallet's whole entitlement, the fee its own transfer
        consumed included, because the debt is decremented by the outputs *and* the fee.
        """
        catalogue, sent = self._pay(
            11_000_000,
            wallets=[Wallet(WALLET_A, Decimal("0.7")), Wallet(WALLET_B, Decimal("0.3"))],
            min_transfer=0,
        )
        [settlement] = catalogue.settlements
        self.assertEqual(
            settlement["credited"], [(WALLET_A, 7_700_000), (WALLET_B, 3_300_000)]
        )
        self.assertEqual(
            sum(amount for _, amount in settlement["credited"]),
            settlement["paid_native"],
            "the credits have to add up to what the debt was decremented by",
        )

    def test_a_wallet_already_credited_its_share_is_not_paid_again(self):
        # Its entitlement is what it is owed *minus* what it has had, so a wallet that
        # has already received its weight of everything accrued gets nothing until more
        # is earned -- and the other wallet takes what is left.
        catalogue, sent = self._pay(
            11_000_000,
            wallets=[Wallet(WALLET_A, Decimal("0.5")), Wallet(WALLET_B, Decimal("0.5"))],
            min_transfer=0,
            paid={WALLET_A: 11_000_000},
        )
        self.assertEqual(sent[0]["receivers"], [WALLET_B])

    def test_a_small_weight_is_paid_across_ticks_rather_than_never(self):
        """The fix, through the real payout rather than through the arithmetic alone.

        1 % of a 11e6 debt is below Ergo's minimum box value, so the first tick pays
        only the big wallet. What the small wallet was owed stays owed *to it*, so a few
        ticks later its entitlement clears the floor and it is paid.
        """
        wallets = [Wallet(WALLET_A, Decimal("0.99")), Wallet(WALLET_B, Decimal("0.01"))]
        paid, received = {}, {WALLET_A: 0, WALLET_B: 0}
        owed = Decimal(0)
        for _ in range(12):
            owed += Decimal(11_000_000)
            catalogue, sent = self._pay(owed, wallets=wallets, min_transfer=0, paid=paid)
            if not sent:
                continue
            for address, amount in zip(sent[0]["receivers"], sent[0]["amount"]):
                received[address] += int(round(amount * 10 ** 9))
            paid = catalogue.paid
            owed = catalogue.owed
        self.assertGreater(received[WALLET_B], 0,
                           "the 1 % wallet was never paid anything at all")

    def test_an_unreadable_credit_map_pays_nothing_rather_than_guessing(self):
        """The direction a read failure has to fail in, and it is not the obvious one.

        An empty map and an unreadable one are different facts. Read as "nobody has
        been paid", a transient failure hands an accumulated claim to whichever wallets
        clear the floor today -- an address that has been unpayable for a month is owed
        the lot, and would get half of it. Nothing is lost by waiting a tick, so the
        payout aborts before anything is broadcast.
        """
        unreadable = _Catalogue(11_000_000)
        unreadable.donation_paid_by_address = lambda *_args: None
        catalogue, sent = self._pay(11_000_000, min_transfer=0, catalogue=unreadable)
        self.assertEqual(sent, [], "nothing may be broadcast without the credit map")
        self.assertEqual(catalogue.settlements, [])
        self.assertEqual(catalogue.owed, 11_000_000, "and the debt is untouched")

    def test_the_credit_is_written_against_the_map_the_payout_planned_with(self):
        """Not against a fresh read, which is the window that costs a double payment.

        The credit is written after the transaction is on the wire, where a failed read
        can abort nothing -- so reading again there would write each credit as though
        the wallet had never been paid, wipe its history, and hand it its whole share
        again on a later tick.
        """
        catalogue, _sent = self._pay(
            11_000_000, wallets=[Wallet(WALLET_A, Decimal(1))], min_transfer=0,
            paid={WALLET_A: 5_000_000},
        )
        [settlement] = catalogue.settlements
        # 5e6 already credited plus this payout's 11e6 entitlement.
        self.assertEqual(catalogue.paid[WALLET_A], Decimal(16_000_000))
        self.assertEqual(settlement["credited"], [(WALLET_A, 11_000_000)])

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
