"""The peer-selection score: price, reliability, donations, and preferring ourselves.

The score is read as an effective cost in log space, which is what makes each weight a
*maximum equivalent discount*: since price enters as a logarithm, a weight of W means
the best possible candidate on that term beats a price up to e**W higher, and never
more. These tests pin that ceiling, the sign of each term, and the one behaviour that
must not change while donations are added -- how much this node prefers itself.
"""
import unittest
from math import e, exp

IMPORT_ERROR = None
try:
    from src.balancers.scoring import reputation_factor, score
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

W_R = 2.0
W_D = 0.3
LOCAL_BIAS = 1.0


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReputationFactorTests(unittest.TestCase):

    def test_half_the_weight_is_earned_at_the_half_credit(self):
        self.assertAlmostEqual(reputation_factor(50, 50), 0.5)

    def test_it_saturates_without_reaching_one(self):
        self.assertLess(reputation_factor(10 ** 9, 50), 1.0)
        self.assertGreater(reputation_factor(10 ** 9, 50), 0.999)

    def test_a_peer_that_failed_us_is_still_penalised(self):
        """Sign-preserving, unlike the donation term.

        Reputation measures behaviour, and a peer that refused our calls should be
        ranked below one we know nothing about. Donations are the opposite case: a
        bonus only, because penalising non-donors is what would make patching
        donations out of a node rational.
        """
        self.assertEqual(reputation_factor(-50, 50), -0.5)
        self.assertLess(
            score(cost_mu=1000, reputation=-50, reputation_weight=W_R),
            score(cost_mu=1000, reputation=0, reputation_weight=W_R),
        )

    def test_it_does_not_depend_on_how_many_peers_exist(self):
        # The shape this replaced divided by the network's total reputation, so a peer's
        # standing moved when an unrelated peer was introduced. Nothing here is passed
        # anything about other candidates.
        self.assertEqual(reputation_factor(50, 50), reputation_factor(50, 50))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ScoreTests(unittest.TestCase):

    def test_a_free_candidate_is_the_best_offer_and_not_a_domain_error(self):
        self.assertEqual(score(cost_mu=0), float('inf'))

    def test_price_alone_orders_two_identical_peers(self):
        self.assertGreater(score(cost_mu=100), score(cost_mu=200))

    def test_the_donation_weight_is_the_maximum_price_premium(self):
        """W_d = 0.3 buys a generous tie-break and nothing more.

        The largest imaginable donor (bonus -> 1) beats a rival priced up to e**0.3
        higher -- about 35 % -- and loses to one any cheaper than that. Nobody buys
        dominance.
        """
        donor = lambda cost: score(cost_mu=cost, donation_bonus=1.0, donation_weight=W_D)
        plain = lambda cost: score(cost_mu=cost)

        # 30 % dearer: the donation still wins.
        self.assertGreater(donor(130), plain(100))
        # 40 % dearer, past the e**0.3 ceiling: price wins.
        self.assertLess(donor(140), plain(100))
        self.assertAlmostEqual(exp(W_D), 1.3498588, places=6)

    def test_a_peer_with_no_donation_is_not_penalised(self):
        self.assertEqual(score(cost_mu=100, donation_weight=W_D, donation_bonus=0.0),
                         score(cost_mu=100))

    def test_local_bias_of_one_reproduces_todays_delegation_policy(self):
        """The regression this issue must not quietly change.

        Today `local` gets a flat reputation of 1 while peers get (rep/total)*2 -- in
        practice ~0.2 with ten similar peers -- so local already tolerates a price up
        to e**1 (~2.7x) higher than a peer's. That policy is now an explicit setting
        with the same value, so the decision has to come out the same.
        """
        local = lambda cost: score(cost_mu=cost, local_bias=LOCAL_BIAS)
        peer = lambda cost: score(cost_mu=cost, reputation=0, reputation_weight=W_R)

        # 2.5x the peer's price: still worth running here.
        self.assertGreater(local(250), peer(100))
        # 3x: delegate it.
        self.assertLess(local(300), peer(100))
        self.assertAlmostEqual(e, 2.718281828, places=6)

    def test_local_bias_of_zero_makes_this_node_just_another_candidate(self):
        self.assertEqual(score(cost_mu=100, local_bias=0.0), score(cost_mu=100))

    def test_our_own_donations_count_for_us_too(self):
        # Read off the chain with the same list as everyone else's, so a node that
        # funds people it also counts is not disfavoured against the peers it competes
        # with. `local_bias` replaces the reputation term, not the donation one.
        with_credit = score(cost_mu=100, local_bias=LOCAL_BIAS,
                            donation_bonus=1.0, donation_weight=W_D)
        without = score(cost_mu=100, local_bias=LOCAL_BIAS)
        self.assertGreater(with_credit, without)

    def test_local_gets_no_reputation_of_its_own(self):
        """We hold no evidence about ourselves.

        `local_bias` is present, so whatever reputation is passed is ignored: a node's
        reputation table says nothing about the node keeping it, and home-field
        preference is a policy rather than a score this node awards itself.
        """
        self.assertEqual(
            score(cost_mu=100, local_bias=LOCAL_BIAS, reputation=10 ** 6, reputation_weight=W_R),
            score(cost_mu=100, local_bias=LOCAL_BIAS),
        )

    def test_a_max_donor_still_loses_to_a_far_cheaper_peer(self):
        best = score(cost_mu=1000, reputation=10 ** 6, reputation_weight=W_R,
                     donation_bonus=1.0, donation_weight=W_D)
        cheap = score(cost_mu=10, reputation=0, reputation_weight=W_R)
        self.assertLess(best, cheap, "money must not buy past a tenth of the price")


if __name__ == "__main__":
    unittest.main()
