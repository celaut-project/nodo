"""The peer-selection score. Pure arithmetic: no database, no config, no network.

A candidate is ranked by an **effective cost in log space**, with everything else as a
bounded discount on it:

    score(peer)  = -ln(cost_mu) + W_r * r + W_o * o + W_d * d
    score(local) = -ln(cost_mu) + LOCAL_BIAS       + W_d * d

sorted descending. Reading it that way is what makes the weights meaningful: since
price enters as a logarithm, a weight *is* the maximum equivalent discount. ``W_d =
0.3`` means the best conceivable donor beats a price up to ``e**0.3`` -- about 35 % --
higher, and never more. The ceiling is one number and can be explained in a sentence.

There are two reputation terms because there are two reputations, and only one of them
is this node's own observation. ``r`` is what we saw ourselves and nobody can buy; ``o``
is what the ledgers say, which **is** bought -- an opinion is worth ``share x burned
ERG``. They therefore cannot share a weight. ``W_o <= W_d`` by construction (config
validation refuses the other order), because ``W_o / W_d`` is the exchange rate between
destroying an ERG and donating one, and it has to favour donating: the money in a
donation funds the development this node runs on, and the money in a burn is gone.
Issue #353.

The shape this replaced could not do that. Reputation entered as
``(rep / total_network_reputation) * 2``, so with ten similar peers it was worth about
0.2 against a ``log(cost_mu)`` of ~14: price decided every comparison and reputation
only broke near-exact ties. It also made a peer's standing depend on how many peers
existed, and it compared ``local`` -- a flat 1 -- against a scale nothing else was on.
"""
from __future__ import annotations

from math import log
from typing import Optional

# Reputation at which half the reputation weight is earned. A default here as well as
# in the config so this module stays callable on its own.
DEFAULT_REPUTATION_HALF_CREDIT = 50.0


def reputation_factor(reputation: float, half_credit: float = DEFAULT_REPUTATION_HALF_CREDIT) -> float:
    """``r / (|r| + half)`` in (-1, 1). Sign-preserving, and saturating both ways.

    Sign-preserving because a peer with negative reputation has failed us and should
    still be penalised -- unlike donations, which are a bonus only. Saturating because
    a peer that has behaved well ten thousand times is not a hundred times the peer
    that behaved well a hundred times, and because a runaway term would let reliability
    override price without limit.

    No division by the network total, which is what used to make one peer's standing
    depend on how many other peers this node happened to know.
    """
    half = abs(float(half_credit)) or DEFAULT_REPUTATION_HALF_CREDIT
    value = float(reputation)
    return value / (abs(value) + half)


def score(
    *,
    cost_mu: float,
    reputation: float = 0.0,
    reputation_weight: float = 0.0,
    reputation_half_credit: float = DEFAULT_REPUTATION_HALF_CREDIT,
    donation_bonus: float = 0.0,
    donation_weight: float = 0.0,
    onchain_reputation: float = 0.0,
    onchain_weight: float = 0.0,
    local_bias: Optional[float] = None,
) -> float:
    """Rank one candidate. Higher is better.

    ``local_bias`` present means this candidate is *this node*: the home-field
    preference replaces the reputation term rather than adding to it, because a node
    holds no evidence about itself and its own reputation table says nothing about the
    node keeping it. Preferring to run work locally is a policy, so it is its own
    named term instead of a reputation this node invents for itself.

    The on-chain term goes with it, and for the same reason: what the ledgers say about
    this node is what this node published, so counting it here would let an operator
    raise their own rank against their peers by burning ERG into a proof.

    ``onchain_reputation`` arrives already bounded in ``(-1, 1)`` and already weighed by
    how much this node trusts each publisher -- see
    :mod:`src.reputation_system.onchain_credit`. Nothing that can be bought is added to
    it here; the saturation it went through is what makes the weight a ceiling.

    ``cost_mu == 0`` is ``+inf``: a peer giving its capacity away is the best offer
    there is, not a math domain error. ``log(0)`` used to raise here, and a price of
    zero became reachable when a node could serve a free tier.
    """
    if cost_mu <= 0:
        return float('inf')

    total = -log(float(cost_mu))
    if local_bias is None:
        total += float(reputation_weight) * reputation_factor(reputation, reputation_half_credit)
        total += float(onchain_weight) * float(onchain_reputation)
    else:
        total += float(local_bias)
    # Bonus only: donation weights are refused if negative (config validation), so this
    # term can never push a candidate below where its price and reliability put it.
    total += float(donation_weight) * float(donation_bonus)
    return total
