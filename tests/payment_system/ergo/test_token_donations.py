"""#282's donation circuit, per asset, on one tick.

The debts are already per payment method: #282 keyed `donation_accrual` by
`(ledger, contract, asset)` precisely so this issue is additive. What is added here is
that one Ergo contract now owes several of them at once, and that paying them is one
transaction rather than one per asset -- each extra transaction being a fee the node
donates on top of the share its operator configured.

The ERG in a token payout is the part worth reading twice: an Ergo fee is paid in ERG,
and a debt in SigUSD cannot pay it. So ERG's debt declares the fee (#282's promise that
the fee comes out of the share) and a token's declares none, which means a token payout
costs this node ERG it never accrued.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.ergo import interface, rate
    from src.payment_system.donations import payout
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    interface = None  # type: ignore[assignment]

TOKEN = "ab" * 32
WALLET_A = "9walletAAA"
WALLET_B = "9walletBBB"


def _asset(decimals=2):
    return rate.Asset(token_id=TOKEN, symbol="SigUSD", unit_name="sigusd",
                      decimals=decimals, mu_per_base_unit=Decimal(1))


class _Ledger:
    """The debt rows, and what was settled against them."""

    def __init__(self, owed, paid=None):
        self.owed = dict(owed)
        # Per asset, then per wallet: a credit in nanoERG says nothing about what a
        # wallet has had in a token, so the two debts are kept apart here as well.
        self.paid = {asset: dict(by_address) for asset, by_address in (paid or {}).items()}
        self.settled = []

    def donation_owed(self, ledger, contract_hash, token_id):
        return Decimal(self.owed.get(token_id, 0))

    def donation_paid_by_address(self, ledger, contract_hash, token_id):
        return dict(self.paid.get(token_id, {}))

    def settle_donations(self, entries):
        self.settled.append(entries)
        for entry in entries:
            self.owed[entry["token_id"]] = (
                Decimal(self.owed.get(entry["token_id"], 0))
                - Decimal(entry["paid_native"])
            )
            by_address = self.paid.setdefault(entry["token_id"], {})
            # Against the map the payout planned with, exactly as the real one does.
            base = entry.get("paid_before") or {}
            for address, amount in entry.get("credited") or ():
                by_address[address] = Decimal(str(base.get(address, 0))) + Decimal(amount)
        return True

    def settle_donation(self, **entry):
        return self.settle_donations([entry])



@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TokenDonationPayoutTests(unittest.TestCase):
    def setUp(self):
        self.logged = []
        self.sent = []
        for patcher in (
            mock.patch.object(interface, "LOGGER", self.logged.append),
            mock.patch.object(payout, "LOGGER", self.logged.append),
            mock.patch.object(interface, "WALLET_MNEMONIC", lambda: "words"),
            mock.patch.object(interface, "__get_sender_addr", return_value=mock.Mock()),
            mock.patch.object(interface, "SIMULATE_PAYMENTS", lambda: False),
            mock.patch.object(
                interface, "_send_assets",
                side_effect=lambda outputs, fee_nanoerg: (
                    self.sent.append((outputs, fee_nanoerg)) or "tx-1"
                ),
            ),
            mock.patch("src.utils.ergo_units.is_valid_ergo_address", lambda _a: True),
            mock.patch.object(interface, "is_valid_ergo_address", lambda _a: True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _pay(self, owed, *, assets=(), wallets=(WALLET_A,), nanoergs=10 * 10**9,
             tokens=(), min_transfers=None, paid=None, ledger=None):
        ledger = ledger if ledger is not None else _Ledger(owed, paid)
        min_transfers = min_transfers or {}
        balance = {"confirmed": {"nanoErgs": nanoergs, "tokens": [
            {"tokenId": token_id, "amount": amount} for token_id, amount in tokens
        ]}}
        pay_list = [payout.donation_config.Wallet(address=a, weight=Decimal(1))
                    for a in wallets]
        import src.database.sql_connection as sql_module

        with mock.patch.object(rate, "assets", return_value=tuple(assets)), \
                mock.patch.object(interface, "__balance_total", return_value=balance), \
                mock.patch.object(sql_module, "SQLConnection", lambda: ledger), \
                mock.patch.object(payout.donation_config, "pay_wallets",
                                  return_value=pay_list), \
                mock.patch.object(
                    payout.donation_config, "min_transfer",
                    side_effect=lambda _l, asset: Decimal(min_transfers.get(asset, 0))):
            interface._pay_accrued_donations()
        return ledger

    def test_two_debts_are_paid_in_one_transaction(self):
        ledger = self._pay(
            {"ERG": Decimal(2 * 10**9), TOKEN: Decimal(5_000)},
            assets=[_asset()], tokens=[(TOKEN, 5_000)],
        )
        self.assertEqual(len(self.sent), 1, "one tick must be one transaction")
        [(outputs, fee)] = self.sent
        [(address, nanoerg, token_outputs)] = outputs
        self.assertEqual(address, WALLET_A)
        # ERG's debt paid the fee, so what goes out is the debt minus it.
        self.assertEqual(nanoerg, 2 * 10**9 - interface.DEFAULT_FEE)
        self.assertEqual(token_outputs, [(TOKEN, 5_000)])
        self.assertEqual(fee, interface.DEFAULT_FEE)

    def test_both_debts_are_discharged_in_one_database_transaction(self):
        # The window between two commits is exactly where a crash pays one debt twice.
        ledger = self._pay(
            {"ERG": Decimal(2 * 10**9), TOKEN: Decimal(5_000)},
            assets=[_asset()], tokens=[(TOKEN, 5_000)],
        )
        self.assertEqual(len(ledger.settled), 1)
        self.assertEqual(
            sorted(entry["token_id"] for entry in ledger.settled[0]),
            sorted(["ERG", TOKEN]),
        )

    def test_a_token_debt_is_paid_in_the_tokens_own_base_unit(self):
        # Not in MU, and not in ERG: 5000 cents of SigUSD is what leaves.
        ledger = self._pay(
            {TOKEN: Decimal(5_000)}, assets=[_asset()], tokens=[(TOKEN, 5_000)],
        )
        [(outputs, _fee)] = self.sent
        self.assertEqual(outputs[0][2], [(TOKEN, 5_000)])
        [entry] = ledger.settled[0]
        self.assertEqual(entry["token_id"], TOKEN)
        self.assertEqual(entry["paid_native"], 5_000)

    def test_a_token_only_payout_pays_for_its_own_carrier_box(self):
        # No ERG debt, so nothing accrued covers the box the tokens land in. The node
        # pays it, and says so rather than letting the ERG leave silently.
        self._pay({TOKEN: Decimal(5_000)}, assets=[_asset()], tokens=[(TOKEN, 5_000)])
        [(outputs, _fee)] = self.sent
        self.assertEqual(outputs[0][1], interface.SAFE_MIN_BOX_VALUE)
        self.assertIn("carrier boxes", " ".join(self.logged))

    def test_each_wallet_gets_one_box_carrying_everything_it_is_owed(self):
        # Not one box per wallet per asset: the same output carries both.
        self._pay(
            {"ERG": Decimal(4 * 10**9), TOKEN: Decimal(5_000)},
            assets=[_asset()], wallets=(WALLET_A, WALLET_B), tokens=[(TOKEN, 5_000)],
        )
        [(outputs, _fee)] = self.sent
        self.assertEqual(len(outputs), 2)
        for _address, nanoerg, tokens in outputs:
            self.assertGreater(nanoerg, 0)
            self.assertEqual(len(tokens), 1)

    def test_a_debt_in_tokens_the_wallet_no_longer_holds_is_not_paid(self):
        # Deposits come out of the same wallet, so the funds can be gone by the tick.
        ledger = self._pay(
            {TOKEN: Decimal(5_000)}, assets=[_asset()], tokens=[(TOKEN, 10)],
        )
        self.assertEqual(self.sent, [])
        self.assertEqual(ledger.settled, [])
        self.assertIn("does not hold them", " ".join(self.logged))

    def test_a_wallet_without_erg_cannot_pay_a_token_donation(self):
        ledger = self._pay(
            {TOKEN: Decimal(5_000)}, assets=[_asset()], tokens=[(TOKEN, 5_000)],
            nanoergs=1_000,
        )
        self.assertEqual(self.sent, [])
        self.assertEqual(ledger.settled, [])
        self.assertIn("the wallet holds", " ".join(self.logged))

    def test_a_debt_below_its_minimum_transfer_stays_accrued(self):
        ledger = self._pay(
            {TOKEN: Decimal(50)}, assets=[_asset()], tokens=[(TOKEN, 50)],
            min_transfers={TOKEN: "1"},
        )
        self.assertEqual(self.sent, [])
        self.assertEqual(ledger.owed[TOKEN], Decimal(50))

    def test_only_the_asset_that_is_owed_is_paid(self):
        ledger = self._pay(
            {"ERG": Decimal(2 * 10**9)}, assets=[_asset()], tokens=[(TOKEN, 5_000)],
        )
        [(outputs, _fee)] = self.sent
        self.assertEqual(outputs[0][2], [])
        self.assertEqual([e["token_id"] for e in ledger.settled[0]], ["ERG"])

    def test_nothing_owed_sends_nothing(self):
        ledger = self._pay({}, assets=[_asset()], tokens=[(TOKEN, 5_000)])
        self.assertEqual(self.sent, [])
        self.assertEqual(ledger.settled, [])

    def test_each_asset_credits_its_wallets_in_its_own_unit(self):
        """The per-wallet ledger is per asset too, and cannot be shared across them.

        A wallet credited 5000 base units of SigUSD has had nothing in ERG. Merged into
        one counter, its ERG entitlement would look already paid -- and it would stop
        being funded in ERG entirely.
        """
        ledger = self._pay(
            {"ERG": Decimal(2 * 10**9), TOKEN: Decimal(5_000)},
            assets=[_asset()], tokens=[(TOKEN, 5_000)],
        )
        [entries] = ledger.settled
        credited = {entry["token_id"]: dict(entry["credited"]) for entry in entries}
        self.assertEqual(credited[TOKEN], {WALLET_A: 5_000})
        self.assertEqual(credited["ERG"], {WALLET_A: 2 * 10**9})

    def test_a_wallet_already_credited_a_token_is_not_paid_it_again(self):
        """Its entitlement in that asset is spent, so the other wallet takes the debt.

        With one wallet this says nothing: a live debt plus what that wallet has had
        means it is simply owed the newer part. The claim only has content with two --
        A has had its half of everything, so what is owed now is B's.
        """
        ledger = self._pay(
            {TOKEN: Decimal(10_000)}, assets=[_asset()],
            wallets=(WALLET_A, WALLET_B), tokens=[(TOKEN, 10_000)],
            paid={TOKEN: {WALLET_A: 10_000}},
        )
        [(outputs, _fee)] = self.sent
        self.assertEqual([address for address, _n, _t in outputs], [WALLET_B])
        [entry] = ledger.settled[0]
        self.assertEqual(dict(entry["credited"]), {WALLET_B: 10_000})

    def test_an_unreadable_credit_map_pays_no_asset_rather_than_guessing(self):
        # Per asset: what cannot be read for one asset stops that asset's payout, and
        # an unreadable map is not "nobody has been paid" -- read that way, a claim
        # accumulated by an unpayable address goes to whoever clears the floor today.
        unreadable = _Ledger({"ERG": Decimal(2 * 10**9), TOKEN: Decimal(5_000)})
        unreadable.donation_paid_by_address = lambda *_args: None
        self._pay({}, assets=[_asset()], tokens=[(TOKEN, 5_000)], ledger=unreadable)
        self.assertEqual(self.sent, [])
        self.assertEqual(unreadable.settled, [])

    def test_simulate_payments_broadcasts_nothing_and_keeps_the_debts(self):
        with mock.patch.object(interface, "SIMULATE_PAYMENTS", lambda: True):
            ledger = self._pay(
                {"ERG": Decimal(2 * 10**9), TOKEN: Decimal(5_000)},
                assets=[_asset()], tokens=[(TOKEN, 5_000)],
            )
        self.assertEqual(self.sent, [])
        self.assertEqual(ledger.settled, [])
        self.assertIn("SIMULATE_PAYMENTS is on", " ".join(self.logged))


if __name__ == "__main__":
    unittest.main()
