"""On-chain reputation, turned into one bounded number per peer for the balancer.

The other reputation. :func:`src.reputation_system.interface.compute_reputation` is what
*this node* observed -- our own event log, unpurchasable because the only way to raise it
is to take our payments and answer our calls. This module reads what the **ledgers** say,
which is a different quantity that shares the name and **is** bought: an opinion is worth
``share x burned ERG``, minting a proof is free, and the ERG put into one can never come
back out (``docs/ERGO.md``). Importing that figure as it stands would make burning ERG a
strictly better buy than donating, which is the incentive ``docs/DONATIONS.md`` is built
on. Issue #353.

So it is not imported as it stands. What is imported is an opinion's **credibility to
us**, not its price:

    trust(p) = max(0, r_local(p))                       in [0, 1)
    v(p)     = sum of p's signed shares on the subject   in [-1, 1]
    c(p)     = trust(p) * sign(v(p)) * min(|v(p)|, cap)
    S        = sum over publishers of c(p)
    o        = S / (|S| + ONCHAIN_REPUTATION_HALF_CREDIT)  in (-1, 1)

Three properties fall out of that shape, and each one is the answer to a way of buying
this term:

* **``burned_nanoerg`` never appears.** A proof's sacrifice buys the credibility of the
  judgements *it publishes* -- ``burned_by_proof`` keys on ``opinion.proof_id``, the
  publishing proof -- and this node prices that credibility itself, out of its own
  ledger of how that publisher has behaved towards it. Burning into a proof nobody here
  has transacted with buys ``trust(p) = 0``, at any price.
* **A publisher we distrust is silenced, not inverted.** ``max(0, ...)``: were a negative
  local score to flip an opinion's sign, paying a peer we already distrust to badmouth a
  rival would *promote* the rival, and slandering yourself from a burner proof would be a
  way to raise your own standing.
* **No single publisher decides.** Each is capped at ``ONCHAIN_PUBLISHER_CAP`` of the
  sum, so the term is concave per publisher as well as in aggregate -- one trusted peer
  that has been bought moves a candidate by a fraction of the weight, never by all of it.

The cost of this shape is honest and worth stating: **an unknown rater counts for
nothing**, so a node whose only vouchers are peers we have never dealt with gets zero
here. That is the bootstrapping price of making the term a transitive extension of local
observation rather than a second purchasable channel. Newcomers are what ``LOCAL_BIAS``
and the donation term are for.

``trust`` reuses ``REPUTATION_HALF_CREDIT`` rather than adding a knob of its own,
deliberately: a publisher lends us exactly the credibility our own ranking already gives
it, and never more.

Everything here is computed from rows already in SQLite -- a routing decision does no
network I/O at all. The chain is read on the periodic tick (:mod:`.onchain_indexer`), and
if that has never succeeded every candidate's standing is zero: all of them, never some,
because a half-filled index would silently favour whichever peers happen to be cached.
"""
from __future__ import annotations

from time import monotonic
from typing import Dict, Iterable, Mapping, Optional, Tuple

from src.balancers.scoring import DEFAULT_REPUTATION_HALF_CREDIT, reputation_factor
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

# Weight of the on-chain term. **At or below ``DONATION_WEIGHT`` by construction** --
# config validation refuses the other order -- because the ratio between the two is the
# exchange rate between "destroy 1 ERG" and "donate 1 ERG", and it has to favour
# donating. At the shipped 0.1 against 0.3, the entire on-chain ceiling is worth what
# 2.5 ERG donated is worth, and no amount of burning reaches past it.
DEFAULT_ONCHAIN_WEIGHT = 0.1

# Summed, trusted, capped verdict at which half the weight is earned. 1.0 means four
# fully-trusted publishers at the cap, which is a real coalition rather than a purchase.
DEFAULT_ONCHAIN_HALF_CREDIT = 1.0

# The most any one publisher may contribute to the sum. A quarter, so the half credit
# cannot be reached by fewer than four independent trusted publishers.
DEFAULT_PUBLISHER_CAP = 0.25

# How long a computed set of standings is reused. Same window and same reason as the
# donation credit's: the index behind it only changes on the hourly scan, and a burst of
# launches should cost one pass rather than one each.
CACHE_SECONDS = 60
_cache: Optional[Tuple[float, Dict[str, float]]] = None


def _parameter(key: str, default: float) -> float:
    """One parameter of the on-chain term, from ``balancers:``.

    Validated at load (``utils.config_validation.validate_balancers_config``), so a value
    that gets here malformed means the config changed under a running node. The default
    is used rather than raising: a routing decision must still complete.
    """
    raw = ConfigManager().get(f"balancers.{key}", default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        LOGGER(f"balancers.{key}={raw!r} is not a number; using {default}.")
        return float(default)
    return value


def weight() -> float:
    return _parameter("ONCHAIN_REPUTATION_WEIGHT", DEFAULT_ONCHAIN_WEIGHT)


def half_credit() -> float:
    value = _parameter("ONCHAIN_REPUTATION_HALF_CREDIT", DEFAULT_ONCHAIN_HALF_CREDIT)
    return value if value > 0 else DEFAULT_ONCHAIN_HALF_CREDIT


def publisher_cap() -> float:
    value = _parameter("ONCHAIN_PUBLISHER_CAP", DEFAULT_PUBLISHER_CAP)
    return value if value > 0 else DEFAULT_PUBLISHER_CAP


def trust(local_reputation: float, half: float = DEFAULT_REPUTATION_HALF_CREDIT) -> float:
    """How much of a publisher's opinion this node is willing to hear, in ``[0, 1)``.

    The positive half of the very factor the balancer already ranks that publisher by, so
    a peer lends us exactly the credibility we would give it ourselves. A peer we have
    never dealt with scores 0 and is therefore inaudible -- which is the whole defence:
    a self-funded proof belongs to no peer we have transacted with, so it weighs nothing
    no matter how much was burned into it.

    Clamped at zero rather than sign-preserving. A negative local score means *we do not
    listen*, not *we believe the opposite*: inverting a distrusted peer's verdict would
    make paying a peer we already distrust to speak against a rival a way to promote that
    rival, and speaking against yourself a way to raise your own standing.
    """
    return max(0.0, reputation_factor(local_reputation, half))


def contribution(verdict: float, publisher_trust: float, cap: float) -> float:
    """One publisher's contribution to the sum: trusted, capped, sign-preserving.

    ``verdict`` is the share of its own proof the publisher has staked on this node,
    already netted across its boxes (``opinions.by_proof``), so it is in ``[-1, 1]``. The
    cap applies to the magnitude and the sign survives it: a proof that stakes all of
    itself against a peer says something worth hearing, it just does not get to say it
    four times over.
    """
    if publisher_trust <= 0:
        return 0.0
    magnitude = min(abs(float(verdict)), abs(float(cap)))
    return publisher_trust * magnitude * (1.0 if verdict >= 0 else -1.0)


def standing(
    opinions: Iterable[Mapping[str, object]],
    *,
    trust_by_peer: Mapping[str, float],
    cap: float = DEFAULT_PUBLISHER_CAP,
    half: float = DEFAULT_ONCHAIN_HALF_CREDIT,
) -> Dict[str, float]:
    """``o`` per subject peer. Pure: every input is passed in, nothing is read here.

    ``opinions`` are rows as :meth:`SQLConnection.get_onchain_opinions` returns them --
    one per (subject, publishing proof) that was attributable to a peer this node knows.
    A publisher missing from ``trust_by_peer`` weighs zero, which is the same answer a
    proof belonging to nobody gets.

    One row per proof, and the primary key enforces it: summing per proof before summing
    across publishers is what stops a publisher that split its stake into ten boxes from
    counting ten times, and the indexer does that netting before the rows are written.
    """
    sums: Dict[str, float] = {}
    for row in opinions:
        subject = str(row.get("subject_id") or "")
        publisher = str(row.get("publisher_peer_id") or "")
        if not subject or not publisher:
            continue
        try:
            verdict = float(row.get("verdict") or 0.0)
        except (TypeError, ValueError):
            continue
        value = contribution(verdict, trust_by_peer.get(publisher, 0.0), cap)
        if value:
            sums[subject] = sums.get(subject, 0.0) + value
    return {
        subject: reputation_factor(total, half) for subject, total in sums.items()
    }


def standing_by_peer() -> Dict[str, float]:
    """``o`` per peer, read from SQLite. Never raises, never touches the network.

    An empty result means "the ledgers credit nobody", which is also what a failed index
    and an unreadable database look like -- and that is the intended behaviour: every
    candidate gets zero, never some of them, so a half-filled index cannot tilt a routing
    decision. The donation term is built on the same promise
    (:func:`payment_system.donations.credit.bonus_by_peer`).

    The publishers' trust is read **here**, not frozen into the index, so a peer that
    failed us an hour ago stops lending its credibility on the next routing decision
    rather than at the next chain scan. It is a local table read either way.

    Cached for :data:`CACHE_SECONDS`. A failed computation is never cached: it must not
    blank every candidate's standing for a whole window.
    """
    global _cache
    cached = _cache
    if cached is not None and monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        from src.database.sql_connection import SQLConnection
        from src.reputation_system.interface import compute_reputation

        rows = SQLConnection().get_onchain_opinions()
        if not rows:
            return {}

        half_local = _parameter("REPUTATION_HALF_CREDIT", DEFAULT_REPUTATION_HALF_CREDIT)
        publishers = {
            str(row.get("publisher_peer_id") or "")
            for row in rows
            if row.get("publisher_peer_id")
        }
        trust_by_peer = {
            publisher: trust(compute_reputation(peer_id=publisher), half_local)
            for publisher in publishers
        }

        standings = standing(
            rows,
            trust_by_peer=trust_by_peer,
            cap=publisher_cap(),
            half=half_credit(),
        )
        _cache = (monotonic(), standings)
        return standings
    except Exception as e:
        LOGGER(
            "Could not compute on-chain reputation; treating every candidate as zero: "
            f"{e}"
        )
        return {}


def forget_cached_standings() -> None:
    """Drop the cached standings, so the next read recomputes them.

    For a caller that has just changed what the answer would be -- a fresh scan, or a
    test -- rather than for ordinary use, where the window expiring is the point.
    """
    global _cache
    _cache = None
