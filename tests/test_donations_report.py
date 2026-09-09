"""What `nodo donations` tells an operator, and the one figure that makes it checkable.

A weight is a claim: "0.1 % of what this node earns goes to this address". The report
prints the claim; without printing what has actually reached each wallet it cannot be
checked, and the first version of this circuit got that claim wrong -- a cut too small to
send returned to a debt belonging to nobody and was shared out again on the next tick, so
a small weight was never paid at all and nothing in the output said so.

The figures are per asset, because a credit in nanoERG says nothing about what a wallet
has had in a token.
"""
import io
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.commands import donations as command
    from src.payment_system.donations.config import Wallet
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    command = None  # type: ignore[assignment]

BIG = "9bigWALLET"
SMALL = "9smallWALLET"
TOKEN = "ab" * 32


class _Sql:
    """Only what the report reads."""

    def __init__(self, debts, credits):
        self._debts = debts
        self._credits = credits

    def donation_debts(self):
        return self._debts

    def donation_credits(self):
        return self._credits

    def get_donation_payments(self, limit=1000):
        return [{"ledger": "ergo", "amount_mu": 7_000_000}]

    def donation_scan_tip(self, ledger):
        return 1234


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DonationsReportTests(unittest.TestCase):
    def _report(self, credits):
        debts = [{"ledger": "ergo", "contract_hash": "p2pk", "token_id": "ERG",
                  "owed": Decimal(500)}]
        wallets = [Wallet(BIG, Decimal("0.999")), Wallet(SMALL, Decimal("0.001"))]
        with mock.patch("src.database.sql_connection.SQLConnection",
                        return_value=_Sql(debts, credits)), \
                mock.patch("src.payment_system.contracts.envs.donation_scanners",
                           return_value=["ergo"]), \
                mock.patch("src.payment_system.donations.config.pay_wallets",
                           return_value=wallets), \
                mock.patch("src.payment_system.donations.config.credit_wallets",
                           return_value=[]), \
                mock.patch("src.payment_system.donations.config.credit_weights",
                           return_value={}), \
                mock.patch("src.payment_system.donations.config.percentage",
                           return_value=Decimal("0.02")), \
                mock.patch("src.payment_system.donations.config.min_transfer",
                           return_value=Decimal("0.1")), \
                mock.patch("src.payment_system.donations.config.min_confirmations",
                           return_value=10), \
                mock.patch("src.payment_system.donations.credit.bonus_by_peer",
                           return_value={}), \
                mock.patch("src.payment_system.donations.indexer.unattributed_donors",
                           return_value=[]):
            return command.report(now=0)

    def _printed(self, credits):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            command._print_report(self._report(credits))
        return buffer.getvalue()

    def _credit(self, address, paid, token_id="ERG"):
        return {"ledger": "ergo", "contract_hash": "p2pk", "token_id": token_id,
                "address": address, "paid": Decimal(paid)}

    def test_each_funded_wallet_reports_what_has_reached_it(self):
        [ledger] = self._report([self._credit(BIG, 99_900), self._credit(SMALL, 100)])["ledgers"]
        self.assertEqual(
            [(w["address"], w["paid_native"]) for w in ledger["pay_wallets"]],
            [(BIG, {"ERG": "99900"}), (SMALL, {"ERG": "100"})],
        )

    def test_a_wallet_that_has_never_been_paid_says_so_rather_than_showing_zero(self):
        # "nothing yet" and "0" read the same to a machine and differently to a person:
        # the second invites the question of whether the figure is being tracked at all.
        printed = self._printed([self._credit(BIG, 99_900)])
        self.assertIn("paid so far: 99900", printed)
        self.assertIn("paid so far: nothing yet", printed)

    def test_a_wallet_paid_in_two_assets_reports_them_separately(self):
        # An Ergo address receives anything, so the same wallet is in both pay lists --
        # but the two amounts are different money and cannot be added up.
        [ledger] = self._report([
            self._credit(BIG, 99_900), self._credit(BIG, 42, token_id=TOKEN),
        ])["ledgers"]
        self.assertEqual(
            ledger["pay_wallets"][0]["paid_native"], {"ERG": "99900", TOKEN: "42"}
        )

    def test_the_debt_is_still_reported_next_to_it(self):
        # What is owed and what has been paid answer different questions, and an
        # operator checking a weight needs both.
        report = self._report([self._credit(BIG, 99_900)])
        self.assertEqual(report["ledgers"][0]["owed_native"], {"ERG": "500"})

    def test_a_report_with_no_credits_at_all_still_renders(self):
        # A node that has never paid a donation: every wallet is owed its full share.
        printed = self._printed([])
        self.assertIn("paid so far: nothing yet", printed)
        self.assertIn("weight 0.001", printed)


if __name__ == "__main__":
    unittest.main()
