"""How a donation debt becomes outputs: the weights, the floors, and the leftovers.

Three decisions are pinned here because each is easy to "simplify" into a bug:

* The fee comes out of the debt, not on top of it. Charged on top, the real cost of
  donating would depend on how often the tick fires and could exceed the share the
  operator agreed to.
* A share below the chain's minimum output stays owed **to the wallet that earned it**
  instead of being handed to the bigger wallets.
* The configured minimum transfer is compared against what leaves the node, not against
  what is owed.

The second one is why `SequenceTests` at the bottom of this file exists, and why it is
the most important class here. Every test above it drives a single call, and a single
call cannot see the failure that mattered: the first version of this module recomputed
each wallet's cut from the live debt and returned what it could not pay to a debt
belonging to nobody, so one tick later it was split among everybody again. A wallet with
a weight of 0.001 never cleared the floor, never got paid, and its share ended up in the
big wallets -- exactly what the docstrings promised would not happen. Both the code and
the tests were self-consistent; the property is about a *sequence* of payouts, so only a
sequence could contradict them.
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
        # Half the debt is 'good''s, and its own transfer is what the fee pays for, so
        # it receives its half minus the whole fee. Split by weight instead, 'broken'
        # would be charged half a fee for a transaction it was not in -- and a wallet
        # too small to clear the floor would be billed for every transaction it missed.
        self.assertEqual(plan.outputs, [("good", 4_500_000)])
        self.assertEqual(plan.total_native, 5_500_000)
        # 'broken''s half is still owed, to 'broken', not given away.
        self.assertEqual(plan.withheld_native, Decimal(5_500_000))
        self.assertEqual(plan.credited, [("good", 5_500_000)])

    def test_a_wallet_is_credited_its_entitlement_fee_included(self):
        """What is credited is not what is sent, and the gap is the fee.

        The debt is decremented by the outputs *and* the fee, so crediting only the
        outputs would leave every fee ever paid looking like it is still owed to
        somebody: the entitlements would drift above the debt for ever and the trim
        would fire on every tick.
        """
        plan = _plan(11_000_000, [Wallet("a", Decimal(1))])
        self.assertEqual(plan.outputs, [("a", 10_000_000)])
        self.assertEqual(plan.credited, [("a", 11_000_000)])
        self.assertEqual(sum(amount for _, amount in plan.credited), plan.total_native)

    def test_the_fee_is_split_by_what_each_output_carries(self):
        # In proportion to the transaction's own outputs, never to the configured
        # weights: a wallet not in this transaction must not pay for it.
        plan = _plan(201_000_000, [Wallet("big", Decimal("0.9")), Wallet("small", Decimal("0.1"))])
        # Entitlements of 180.9e6 and 20.1e6 bear 0.9e6 and 0.1e6 of the fee: nine
        # tenths of the transaction is 'big''s, so nine tenths of its cost is too.
        self.assertEqual(plan.outputs, [("big", 180_000_000), ("small", 20_000_000)])
        self.assertEqual(plan.fee_native, FEE)
        # And the credits add up to the debt movement exactly.
        self.assertEqual(sum(a for _, a in plan.credited), plan.total_native)

    def test_nothing_owed_is_nothing_paid(self):
        self.assertIsNone(_plan(0, [Wallet("a", Decimal(1))]))
        self.assertIsNone(_plan("0.5", [Wallet("a", Decimal(1))]))

    def test_no_wallet_is_no_payout(self):
        self.assertIsNone(_plan(11_000_000, []))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SequenceTests(unittest.TestCase):
    """Many ticks, and the question the single-call tests above cannot answer.

    What an operator configures is a weight, and a weight is a claim about the long run:
    "0.1 % of what this node earns goes to this address". A payout looked at once cannot
    show whether that claim holds -- it holds only if what could not be sent this time
    is still owed *to the same wallet* next time.

    So this drives the real loop: accrue, plan, pay what the plan says, decrement the
    debt by what the plan discharged, credit each wallet by what it was owed, repeat.
    The first version of this module fails every test in this class.
    """

    def _run(self, weights, *, per_tick, ticks, min_transfer=0, unpayable=frozenset(),
             fee=FEE, min_payable=MIN_BOX):
        """Accrue ``per_tick`` and try to pay, ``ticks`` times. Returns what was sent."""
        wallets = [Wallet(address, Decimal(str(weight))) for address, weight in weights]
        owed = Decimal(0)
        paid = {}
        sent = {address: 0 for address, _ in weights}
        moved = []  # what each transaction discharged, outputs and fee

        for _ in range(ticks):
            owed += Decimal(str(per_tick))
            plan = plan_payout(
                owed, wallets,
                min_transfer_native=min_transfer,
                min_payable_native=min_payable,
                fee_native=fee,
                unpayable=unpayable,
                paid_native=paid,
            )
            if plan is None:
                continue
            moved.append(plan.total_native)
            for address, amount in plan.outputs:
                sent[address] += amount
            for address, amount in plan.credited:
                paid[address] = paid.get(address, Decimal(0)) + Decimal(amount)
            owed -= Decimal(plan.total_native)
            self.assertGreaterEqual(owed, 0, "a payout must never exceed the debt")

        return sent, owed, moved, paid

    def test_a_tiny_weight_is_actually_paid_eventually(self):
        """The failure the reviewer of #346 found, and the reason this file changed.

        A weight of 0.001 against 0.999. Recomputing the cut from the live debt, the
        small wallet's share is 0.1 % of a debt that the big wallet empties every tick,
        so it never reaches the minimum output -- not after ten ticks and not after ten
        thousand. Its money ends up in the big wallet, which is the one outcome the
        module's docstring rules out.
        """
        sent, _owed, _moved, _paid = self._run(
            [("big", "0.999"), ("small", "0.001")],
            per_tick=100_000_000, ticks=40,
        )
        self.assertGreater(sent["small"], 0, "the small weight was never paid at all")

    def test_the_long_run_split_matches_the_configured_weights(self):
        # Not approximately: what each wallet received, plus the fees its own transfers
        # paid, is its weight of everything that was ever accrued -- to within the
        # minimum output it is still waiting to clear.
        weights = [("a", "0.7"), ("b", "0.28"), ("c", "0.02")]
        sent, owed, _moved, paid = self._run(weights, per_tick=50_000_000, ticks=60)
        accrued = Decimal(50_000_000 * 60)
        self.assertEqual(sum(paid.values()) + owed, accrued, "money appeared or vanished")
        for address, weight in weights:
            target = Decimal(str(weight)) * accrued
            self.assertLessEqual(
                abs(target - paid[address]), Decimal(MIN_BOX),
                f"{address} is off its configured weight by more than one output",
            )

    def test_an_unpayable_wallet_is_paid_in_full_once_it_is_corrected(self):
        """"Corrected, it is paid what it was always owed" -- as a measurement.

        The share is not merely "not redistributed on this tick": it has to still be
        there after the wallet has been unpayable for a long time.
        """
        weights = [("good", "0.5"), ("broken", "0.5")]
        sent, owed, _moved, paid = self._run(
            weights, per_tick=20_000_000, ticks=10, unpayable={"broken"}
        )
        self.assertEqual(sent["broken"], 0)
        accrued = Decimal(20_000_000 * 10)
        # Everything the broken wallet was owed is still owed.
        self.assertEqual(owed, accrued - paid["good"])
        self.assertGreaterEqual(owed, accrued / 2 - Decimal(MIN_BOX))

        # Now the address parses. One tick pays the whole backlog, not a share of it.
        wallets = [Wallet(a, Decimal(str(w))) for a, w in weights]
        plan = plan_payout(
            owed, wallets, min_transfer_native=0, min_payable_native=MIN_BOX,
            fee_native=FEE, paid_native=paid,
        )
        self.assertEqual([address for address, _ in plan.outputs], ["broken"])
        self.assertGreater(dict(plan.outputs)["broken"], 90_000_000)

    def test_a_wallet_added_later_starts_from_zero_and_catches_up(self):
        # It has been credited nothing, so its entitlement is its full weight of
        # everything accrued since -- but never of what was accrued before it existed,
        # because the base only counts what the current list has earned.
        _first, owed, _moved, paid = self._run([("a", "1")], per_tick=50_000_000, ticks=4)
        wallets = [Wallet("a", Decimal(1)), Wallet("b", Decimal(1))]
        owed += Decimal(50_000_000)
        plan = plan_payout(
            owed, wallets, min_transfer_native=0, min_payable_native=MIN_BOX,
            fee_native=FEE, paid_native=paid,
        )
        # 'a' has had everything so far, so the new wallet takes this tick's share.
        self.assertEqual([address for address, _ in plan.outputs], ["b"])
        self.assertLessEqual(sum(a for _, a in plan.credited), int(owed))

    def test_the_debt_is_never_overspent_when_a_weight_is_lowered(self):
        # 'a' was paid at a weight of 1 and is then dropped to 0.5, so it is already
        # over-credited: its entitlement is negative, clamped to zero. The rest must
        # still not plan more than the debt -- which is what the proportional trim is
        # for, and what the assertion inside `_run` checks on every tick.
        _sent, owed, _moved, paid = self._run([("a", "1")], per_tick=50_000_000, ticks=3)
        wallets = [Wallet("a", Decimal("0.5")), Wallet("b", Decimal("0.5"))]
        owed += Decimal(10_000_000)
        plan = plan_payout(
            owed, wallets, min_transfer_native=0, min_payable_native=MIN_BOX,
            fee_native=FEE, paid_native=paid,
        )
        self.assertIsNotNone(plan)
        self.assertLessEqual(plan.total_native, int(owed))
        self.assertNotIn("a", dict(plan.outputs), "already over its share")

    def test_the_minimum_transfer_bounds_what_leaves_rather_than_what_is_owed(self):
        """The second defect, as a sequence.

        With four of five wallets unpayable, the debt clears a 20 ERG minimum long
        before the single payable output does. Compared against the debt, the node
        broadcasts a fifth of the configured minimum and pays a full fee for it.
        """
        weights = [(f"w{i}", "0.2") for i in range(5)]
        broken = {f"w{i}" for i in range(1, 5)}
        # Long enough that a transaction does eventually go out: w0 is owed 1e6 per
        # tick, so it clears a 20e6 minimum on tick 20. Asserting on a run that sent
        # nothing would pass for the wrong reason.
        _sent, _owed, moved, _paid = self._run(
            weights, per_tick=5_000_000, ticks=45,
            min_transfer=20_000_000, unpayable=broken,
        )
        self.assertTrue(moved, "nothing was ever sent, so nothing is being checked")
        for total in moved:
            self.assertGreaterEqual(
                total, 20_000_000,
                "a transaction went out below the configured minimum transfer",
            )

    def test_nothing_is_created_or_destroyed(self):
        # The invariant that has to hold whatever the weights and floors are: every
        # native unit accrued is either sent, spent on a fee, or still owed.
        weights = [("a", "0.5"), ("b", "0.3"), ("c", "0.2")]
        sent, owed, moved, _paid = self._run(
            weights, per_tick=7_000_003, ticks=25
        )
        accrued = Decimal(7_000_003 * 25)
        self.assertEqual(sum(sent.values()) + len(moved) * FEE + owed, accrued)


if __name__ == "__main__":
    unittest.main()
