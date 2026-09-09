"""How a donation debt becomes outputs: the weights, the floors, and the leftovers.

Two decisions are pinned here because both are easy to "simplify" into a bug:

* The fee comes out of the debt, not on top of it. Charged on top, the real cost of
  donating would depend on how often the tick fires and could exceed the share the
  operator agreed to.
* A share below the chain's minimum output stays owed instead of being handed to the
  bigger wallets. Redistributed, a wallet with a small weight would fall below the
  floor every single time, be redistributed every single time, and never be paid at
  all -- its configured weight silently ignored.
"""
import unittest
from decimal import Decimal

IMPORT_ERROR = None
try:
    from src.payment_system.donations.config import Wallet
    from src.payment_system.donations.split import normalised, plan_payout
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    Wallet = None  # type: ignore[assignment]

FEE = 1_000_000
MIN_BOX = 1_000_000


def _plan(owed, wallets, *, min_transfer=0, fee=FEE, min_payable=MIN_BOX):
    return plan_payout(
        Decimal(str(owed)),
        wallets,
        min_transfer_native=min_transfer,
        min_payable_native=min_payable,
        fee_native=fee,
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WeightNormalisationTests(unittest.TestCase):

    def test_weights_are_normalised_to_one(self):
        wallets = normalised([Wallet("a", Decimal(70)), Wallet("b", Decimal(30))])
        self.assertEqual([w.weight for w in wallets], [Decimal("0.7"), Decimal("0.3")])

    def test_unnormalised_weights_pay_exactly_what_their_shares_pay(self):
        """The regression guard: 70/30 must pay what 0.7/0.3 pays.

        Nothing in the config says weights have to sum to anything, so both forms are
        legitimate and they have to mean the same thing.
        """
        raw = _plan(120_000_000, [Wallet("a", Decimal(70)), Wallet("b", Decimal(30))])
        shares = _plan(120_000_000, [Wallet("a", Decimal("0.7")), Wallet("b", Decimal("0.3"))])
        self.assertEqual(raw, shares)

    def test_a_list_whose_weights_are_all_zero_pays_nobody(self):
        # A misconfiguration, refused at startup. Here it must not divide by zero, and
        # must not fall back to splitting evenly among wallets given zero weight.
        self.assertEqual(normalised([Wallet("a", Decimal(0)), Wallet("b", Decimal(0))]), [])
        self.assertIsNone(_plan(120_000_000, [Wallet("a", Decimal(0))]))

    def test_declared_order_is_kept(self):
        # The payout has to be reproducible: which outputs a transaction carries must
        # not depend on set or dict ordering.
        wallets = normalised([Wallet("c", Decimal(1)), Wallet("a", Decimal(1)), Wallet("b", Decimal(1))])
        self.assertEqual([w.address for w in wallets], ["c", "a", "b"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PayoutThresholdTests(unittest.TestCase):

    def test_the_fee_comes_out_of_the_debt(self):
        plan = _plan(11_000_000, [Wallet("a", Decimal(1))])
        self.assertEqual(plan.outputs, [("a", 10_000_000)])
        # What leaves the debt is the output plus the fee, so the operator never pays
        # more than the share they configured.
        self.assertEqual(plan.total_native, 11_000_000)

    def test_a_debt_below_the_fee_and_one_output_is_not_paid_yet(self):
        # fee + one minimum output is 2e6; anything under that would either build an
        # output the network refuses or hand the whole donation to the miner.
        self.assertIsNone(_plan(1_500_000, [Wallet("a", Decimal(1))]))

    def test_the_configured_minimum_transfer_is_respected(self):
        wallets = [Wallet("a", Decimal(1))]
        self.assertIsNone(_plan(50_000_000, wallets, min_transfer=100_000_000))
        self.assertIsNotNone(_plan(150_000_000, wallets, min_transfer=100_000_000))

    def test_the_threshold_scales_with_the_number_of_outputs(self):
        five = [Wallet(str(i), Decimal(1)) for i in range(5)]
        # 5 * 1e6 + 1e6 of fee: exactly enough, and one nanoERG less is not.
        self.assertIsNone(_plan(5_999_999, five))
        self.assertIsNotNone(_plan(6_000_000, five))

    def test_the_threshold_is_native_and_does_not_move_with_the_mu_rate(self):
        """Guards the MU/native mix-up.

        The debt is native and so are the floors, so the same debt clears the same
        threshold whatever ``MU_PER_NANOERG`` is set to. An implementation that read
        the floors out of ``settlement_floors_mu()`` -- which reports MU -- would be
        wrong by exactly that rate: invisible at the default of 1, and silently wrong
        for any operator who changes it.
        """
        wallets = [Wallet("a", Decimal(1))]
        plan = _plan(11_000_000, wallets)
        self.assertEqual(plan.outputs, [("a", 10_000_000)])
        self.assertEqual(plan.fee_native, FEE)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DustAndRemainderTests(unittest.TestCase):

    def test_a_share_below_the_minimum_output_stays_owed(self):
        # 'small' would get 0.0001 of 1e7, which no Ergo box can carry.
        plan = _plan(11_000_000, [Wallet("big", Decimal("0.9999")), Wallet("small", Decimal("0.0001"))])
        self.assertEqual([address for address, _ in plan.outputs], ["big"])
        # And it is not handed to 'big' either: what was not paid is still owed, so
        # 'small' is paid on a later tick once its share clears the floor.
        self.assertLess(plan.total_native, 11_000_000)
        self.assertGreater(plan.withheld_native, 0)

    def test_a_small_weight_is_eventually_paid_rather_than_ignored_forever(self):
        wallets = [Wallet("big", Decimal("0.99")), Wallet("small", Decimal("0.01"))]
        self.assertEqual(
            [address for address, _ in _plan(11_000_000, wallets).outputs],
            ["big"],
            "1 % of 1e7 is below the minimum output",
        )
        # The debt keeps growing because the unpaid share is never written off, so the
        # same 1 % clears the floor on a later tick and both wallets are paid.
        later = _plan(201_000_000, wallets)
        self.assertEqual([address for address, _ in later.outputs], ["big", "small"])

    def test_the_sub_unit_remainder_is_never_paid_and_never_lost(self):
        plan = _plan("11000000.75", [Wallet("a", Decimal(1))])
        # Only whole native units can be moved.
        self.assertEqual(plan.outputs, [("a", 10_000_000)])
        self.assertEqual(plan.total_native, 11_000_000)
        # The fraction stays owed: discarding it on every payout is what would make
        # the node's effective donation rate drift below the configured one.
        self.assertEqual(plan.withheld_native, Decimal("0.75"))

    def test_an_unpayable_wallet_keeps_its_weight_and_its_share_stays_owed(self):
        """An address the chain will not accept must not fund the wallets beside it.

        Dropping it from the list before the split would renormalise the rest -- 0.5
        and 0.5 would become 1.0 -- and pay somebody the operator's weights did not
        aim at. Held, the share goes where it was meant once the address is fixed.
        """
        wallets = [Wallet("broken", Decimal("0.5")), Wallet("good", Decimal("0.5"))]
        plan = plan_payout(
            Decimal(11_000_000),
            wallets,
            min_transfer_native=0,
            min_payable_native=MIN_BOX,
            fee_native=FEE,
            unpayable={"broken"},
        )
        self.assertEqual(plan.outputs, [("good", 5_000_000)])
        # Half the distributable amount is still owed, not given away.
        self.assertEqual(plan.total_native, 6_000_000)
        self.assertEqual(plan.withheld_native, Decimal(5_000_000))

    def test_nothing_owed_is_nothing_paid(self):
        self.assertIsNone(_plan(0, [Wallet("a", Decimal(1))]))
        self.assertIsNone(_plan("0.5", [Wallet("a", Decimal(1))]))

    def test_no_wallet_is_no_payout(self):
        self.assertIsNone(_plan(11_000_000, []))


if __name__ == "__main__":
    unittest.main()
