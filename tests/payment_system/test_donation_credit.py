"""Donation credit: many donations on several chains, one bounded number per peer.

The arithmetic is pinned here because it decides where work is routed, and every part
of it has a failure mode that is invisible in production:

* Unnormalised weights would multiply every credit by their sum and saturate everyone.
* Age measured in blocks would weigh the same donation differently per chain.
* An address dropped from the count list has to stop counting, retroactively.
"""
import unittest
from decimal import Decimal

IMPORT_ERROR = None
try:
    from src.payment_system.donations.credit import age_multiplier, credit_by_peer, saturate
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

YEAR = 31_536_000
SCALE = Decimal(YEAR)
# Ergo's ~120 s block, Bitcoin's ~600 s.
BLOCKS = {"ergo": 120, "bitcoin": 600}
# Both chains priced at 1 MU per base unit, so a credit difference can only come from
# the weights or the ages.
TO_MU = {
    "ergo": lambda amount, asset: amount,
    "bitcoin": lambda amount, asset: amount,
}


def _donation(peer_id="peer-a", ledger="ergo", to_address="pay-me", amount=1_000_000, height=0):
    return {
        "peer_id": peer_id,
        "ledger": ledger,
        "to_address": to_address,
        "token_id": "ERG",
        "amount_native": amount,
        "tx_height": height,
    }


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AgeMultiplierTests(unittest.TestCase):

    def test_the_published_table(self):
        """The figures the config comment and docs/DONATIONS.md quote to an operator."""
        for years, expected in ((0, 1.00), (1, 1.69), (2, 2.10), (5, 2.79)):
            with self.subTest(years=years):
                self.assertAlmostEqual(
                    float(age_multiplier(years * YEAR, SCALE)), expected, places=2
                )

    def test_an_age_from_the_future_counts_as_brand_new(self):
        # A reorg can leave a row above the height this node has indexed. Brand new,
        # not less than nothing.
        self.assertEqual(age_multiplier(-YEAR, SCALE), Decimal(1))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CreditTests(unittest.TestCase):

    def _credit(self, donations, *, weights=None, tips=None):
        return credit_by_peer(
            donations,
            weights=weights if weights is not None else {"ergo": {"pay-me": Decimal(1)}},
            tips=tips if tips is not None else {"ergo": 0, "bitcoin": 0},
            seconds_per_block=BLOCKS,
            to_mu=TO_MU,
            scale=SCALE,
        )

    def test_a_fresh_donation_is_worth_its_face_value(self):
        self.assertEqual(self._credit([_donation()]), {"peer-a": Decimal(1_000_000)})

    def test_normalised_weights_are_what_the_credit_is_scaled_by(self):
        """The saturation trap.

        With weights of 100 -- a perfectly reasonable thing to write -- an
        implementation that skipped normalisation would multiply every credit by 100,
        push every peer to the top of the bonus curve, and make the term rank nobody
        above anybody.
        """
        halves = self._credit(
            [_donation(to_address="a"), _donation(to_address="b")],
            weights={"ergo": {"a": Decimal("0.5"), "b": Decimal("0.5")}},
        )
        self.assertEqual(halves, {"peer-a": Decimal(1_000_000)})

    def test_an_address_no_longer_counted_stops_counting(self):
        # Retroactive by design: the credit is always computed with the list as it
        # stands now, which is also what makes adding an address activate every
        # historical donation to it at once.
        self.assertEqual(self._credit([_donation()], weights={"ergo": {}}), {})

    def test_credit_is_summed_across_chains_in_mu(self):
        credit = self._credit(
            [_donation(ledger="ergo", amount=1_000_000),
             _donation(ledger="bitcoin", to_address="btc-me", amount=500_000)],
            weights={"ergo": {"pay-me": Decimal(1)}, "bitcoin": {"btc-me": Decimal(1)}},
        )
        self.assertEqual(credit, {"peer-a": Decimal(1_500_000)})

    def test_age_is_seconds_and_not_blocks(self):
        """The same number of blocks is a different age on a different chain.

        1000 Ergo blocks are ~33 hours; 1000 Bitcoin blocks are ~7 days. If the age
        scale were in blocks these two donations would be worth the same.
        """
        credit = self._credit(
            [_donation(peer_id="ergo-donor", ledger="ergo", height=0),
             _donation(peer_id="btc-donor", ledger="bitcoin", to_address="btc-me", height=0)],
            weights={"ergo": {"pay-me": Decimal(1)}, "bitcoin": {"btc-me": Decimal(1)}},
            tips={"ergo": 1000, "bitcoin": 1000},
        )
        self.assertGreater(credit["btc-donor"], credit["ergo-donor"])

    def test_a_donation_with_no_donor_credits_nobody(self):
        self.assertEqual(self._credit([_donation(peer_id=None)]), {})

    def test_a_chain_with_no_rate_here_contributes_nothing(self):
        credit = credit_by_peer(
            [_donation(ledger="litecoin")],
            weights={"litecoin": {"pay-me": Decimal(1)}},
            tips={},
            seconds_per_block={},
            to_mu=TO_MU,
            scale=SCALE,
        )
        self.assertEqual(credit, {}, "a chain we cannot value must not be valued anyway")

    def test_no_donations_is_no_credit(self):
        self.assertEqual(self._credit([]), {})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UnavailabilityTests(unittest.TestCase):
    """A donation lookup must never block or tilt a routing decision.

    All or nothing, deliberately: a partial index would silently favour whichever
    peers happen to be cached, which is worse than counting nobody.
    """

    def test_a_catalogue_that_cannot_be_read_gives_every_candidate_zero(self):
        import sys
        import types
        from unittest import mock

        from src.payment_system.donations import credit

        broken = types.ModuleType("src.database.sql_connection")

        def _raise():
            raise RuntimeError("database is gone")

        broken.SQLConnection = _raise  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"src.database.sql_connection": broken}):
            self.assertEqual(credit.bonus_by_peer(), {})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SaturationTests(unittest.TestCase):

    HALF = Decimal(5_000_000_000)

    def test_half_the_bonus_is_earned_at_the_half_credit(self):
        self.assertAlmostEqual(saturate(self.HALF, self.HALF), 0.5)

    def test_the_bonus_never_reaches_one(self):
        absurd = saturate(self.HALF * 10 ** 9, self.HALF)
        self.assertLess(absurd, 1.0)
        self.assertGreater(absurd, 0.99)

    def test_no_credit_is_no_bonus_and_never_a_penalty(self):
        self.assertEqual(saturate(Decimal(0), self.HALF), 0.0)
        # Donations are a bonus only. A negative credit cannot arise -- weights are
        # non-negative -- but it must not become a malus if it ever did.
        self.assertEqual(saturate(Decimal(-1), self.HALF), 0.0)


if __name__ == "__main__":
    unittest.main()
