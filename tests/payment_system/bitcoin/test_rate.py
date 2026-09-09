"""What one MU is worth on Bitcoin, and why there is no default for it.

A satoshi and a nanoERG are about six orders of magnitude apart in value. Copying
Ergo's `MU_PER_NANOERG: 1` by analogy would sell an hour of compute for a millionth of
its price -- the exact failure `docs/PRICING.md` was written to make impossible, and the
one the gas model actually shipped with. So the rate has no default, and a node without
one does not offer Bitcoin at all.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.bitcoin import rate
    from src.utils.config import ConfigManager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    rate = None  # type: ignore[assignment]


def _rate(value):
    manager = ConfigManager()
    real_get = manager.get

    def get(key, default=None):
        if key == rate.RATE_KEY:
            return value
        return real_get(key, default)

    return mock.patch.object(manager, "get", side_effect=get)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RateTests(unittest.TestCase):

    def test_an_unset_rate_is_a_reason_not_an_exception(self):
        # "Should this contract be offered" is a question with an answer. The registry
        # asks it on the payment path and must not have to catch to find out.
        with _rate(""):
            self.assertIn("MU_PER_SATOSHI is not set", rate.rate_reason())
            self.assertEqual(rate.display_units(), {})

    def test_there_is_no_borrowed_default(self):
        with _rate(None):
            self.assertIsNotNone(rate.rate_reason())
            with self.assertRaises(ValueError):
                rate.mu_per_satoshi()

    def test_a_non_positive_or_unreadable_rate_is_refused(self):
        for value in ("0", "-1", "soon"):
            with self.subTest(value=value), _rate(value):
                self.assertIsNotNone(rate.rate_reason())

    def test_conversions_at_a_realistic_rate(self):
        # 2_000_000 MU per satoshi: ERG at $0.50 and BTC at $100,000, with MU_PER_NANOERG
        # at 1. An example, not a quoted price.
        with _rate(2_000_000):
            self.assertEqual(rate.mu_per_satoshi(), Decimal(2_000_000))
            self.assertEqual(rate.mu_per_unit(), 200_000_000_000_000)
            self.assertEqual(rate.satoshi_to_mu(294), 588_000_000)
            self.assertEqual(rate.mu_to_satoshi(2_000_000), 1)

    def test_a_payment_truncates_and_a_debt_does_not(self):
        """The same split Ergo makes, for the same reason.

        A transaction must never claim more than is owed. A donation debt is accrued
        from many payments and paid once, so truncating each conversion would shave a
        sub-satoshi off every one of them -- always in this node's favour.
        """
        with _rate(3):
            self.assertEqual(rate.mu_to_satoshi(10), 3)
            self.assertEqual(rate.mu_to_satoshi_exact(10), Decimal(10) / Decimal(3))

    def test_a_rate_that_does_not_divide_a_whole_btc_is_refused(self):
        # MU is the unit of account; there is nothing smaller than one, so a rate that
        # makes a whole BTC a fractional number of MU cannot be honoured.
        with _rate("0.000000005"):
            self.assertIn("whole number of MU", rate.rate_reason())

    def test_the_display_unit_is_btc_with_eight_decimals(self):
        with _rate(2_000_000):
            units = rate.display_units()
        self.assertEqual(units["btc"]["SYMBOL"], "BTC")
        self.assertEqual(units["btc"]["DECIMALS"], 8)
        self.assertEqual(units["btc"]["MU_PER_UNIT"], Decimal(200_000_000_000_000))


if __name__ == "__main__":
    unittest.main()
