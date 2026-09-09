"""Paying in a token needs two assets, and says which one is missing.

The asymmetry of #342 4.4: a node can be *paid* in a token without ever holding ERG,
because the payer supplies both the fee and the box the token travels in. It cannot
*pay* one, or sweep one, without ERG of its own. "Insufficient balance" on a wallet
visibly holding the token is a message an operator cannot act on, so both shortfalls
are named.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.ergo import interface, rate
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    interface = None  # type: ignore[assignment]

TOKEN = "ab" * 32
UNRELATED = "cd" * 32


def _asset(mu_per_base=1):
    return rate.Asset(token_id=TOKEN, symbol="SigUSD", unit_name="sigusd",
                      decimals=2, mu_per_base_unit=Decimal(mu_per_base))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TokenBalanceTests(unittest.TestCase):
    def setUp(self):
        self.logged = []
        logger = mock.patch.object(interface, "LOGGER", self.logged.append)
        logger.start()
        self.addCleanup(logger.stop)
        sender = mock.patch.object(interface, "__get_sender_addr",
                                   return_value=mock.Mock())
        sender.start()
        self.addCleanup(sender.stop)
        mnemonic = mock.patch.object(interface, "WALLET_MNEMONIC", lambda: "words")
        mnemonic.start()
        self.addCleanup(mnemonic.stop)

    def _balance(self, nanoergs, tokens):
        return {"confirmed": {"nanoErgs": nanoergs, "tokens": tokens}}

    def _check(self, amount_mu, nanoergs, tokens, asset=None):
        with mock.patch.object(
            interface, "__balance_total",
            return_value=self._balance(nanoergs, tokens),
        ) as total:
            self.calls = total
            return interface._token_check_sender_balance(amount_mu, asset or _asset())

    def _enough_erg(self):
        # Fee plus the carrier box plus a change box, with room to spare.
        return interface.DEFAULT_FEE + 3 * interface.SAFE_MIN_BOX_VALUE

    def test_enough_of_both_can_pay(self):
        self.assertTrue(self._check(
            100, self._enough_erg(), [{"tokenId": TOKEN, "amount": 100}]
        ))

    def test_enough_token_and_no_erg_cannot_pay_and_the_message_names_erg(self):
        self.assertFalse(self._check(
            100, 0, [{"tokenId": TOKEN, "amount": 1_000}]
        ))
        message = " ".join(self.logged)
        self.assertIn("ERG for the fee and the carrier box", message)
        self.assertNotIn("SigUSD:", message)

    def test_enough_erg_and_not_enough_token_cannot_pay_and_the_message_names_it(self):
        self.assertFalse(self._check(
            100, self._enough_erg(), [{"tokenId": TOKEN, "amount": 99}]
        ))
        message = " ".join(self.logged)
        self.assertIn("SigUSD:", message)
        self.assertNotIn("ERG for the fee", message)

    def test_a_wallet_holding_only_another_token_cannot_pay(self):
        # Balance is read per asset id, never "the first token in the wallet".
        self.assertFalse(self._check(
            100, self._enough_erg(), [{"tokenId": UNRELATED, "amount": 10**9}]
        ))
        self.assertIn("SigUSD:", " ".join(self.logged))

    def test_the_token_is_found_behind_other_holdings(self):
        self.assertTrue(self._check(100, self._enough_erg(), [
            {"tokenId": UNRELATED, "amount": 5},
            {"tokenId": TOKEN, "amount": 100},
        ]))

    def test_the_requirement_is_converted_through_the_assets_own_rate(self):
        # 20 MU per base unit: 100 MU needs 5 base units, not 100.
        asset = _asset(mu_per_base=20)
        self.assertTrue(self._check(
            100, self._enough_erg(), [{"tokenId": TOKEN, "amount": 5}], asset=asset
        ))
        self.assertFalse(self._check(
            100, self._enough_erg(), [{"tokenId": TOKEN, "amount": 4}], asset=asset
        ))

    def test_one_balance_read_per_check_rather_than_one_per_asset(self):
        # The two figures come out of the same explorer response. Reading it twice
        # would double the calls on the payment path for one decision, and could see
        # two different balances.
        self._check(100, self._enough_erg(), [{"tokenId": TOKEN, "amount": 100}])
        self.assertEqual(self.calls.call_count, 1)

    def test_an_unreadable_balance_refuses_rather_than_assuming_funds(self):
        with mock.patch.object(interface, "__balance_total",
                               side_effect=RuntimeError("explorer unreachable")):
            self.assertFalse(interface._token_check_sender_balance(100, _asset()))
        self.assertIn("explorer unreachable", " ".join(self.logged))


if __name__ == "__main__":
    unittest.main()
