"""On-chain reputation, turned into one bounded number per peer for the balancer.

The other reputation. :func:`src.reputation_system.interface.compute_reputation` is what
*this node* observed -- our own event log, unpurchasable because the only way to raise it
is to take our payments and answer our calls. This module reads what the **ledgers** say,
which is a different quantity that shares the name and **is** bought: an opinion is worth
``share x burned ERG``, minting a proof is free, and the ERG put into one can never come
back out (``docs/ERGO.md``). Importing that figure as it stands would make burning ERG a
strictly better buy than donating, which is the incentive ``docs/DONATIONS.md`` is built
on. Issue #353.

The burn is imported, and what is *not* imported is the assumption that a burn means
anything by itself:

    v(p, s)  = p's netted signed share staked on subject s        in [-1, 1]
    cred(p)  = max(0, cos(v(p, .), r_local(.)))                   in [0, 1]
    c(p, s)  = cred(p) * sign(v) * min(|v(p, s)| * burned_erg(p), ONCHAIN_PUBLISHER_CAP)
    S(s)     = sum over publishing proofs of c(p, s)
    o(s)     = S / (|S| + ONCHAIN_REPUTATION_HALF_CREDIT)          in (-1, 1)

``cred`` is the whole design, so it is worth saying plainly what it is. A reputation
proof is not a peer. A peer is a node we have transacted with and hold an event log
about; a proof is a token that publishes opinions about peers and about other proofs.
We keep local reputation on **peers only** -- that has not changed and does not need to
-- and we work out what a *proof* is worth to us by comparing the opinions it publishes
about peers against the opinions we hold ourselves, over the peers both of us have an
opinion on. The cosine of those two vectors is how much this proof sees the network the
way we see it.

That single number does the job identity cannot:

* **A proof that vouches loudly for a peer that failed us is discredited by that very
  vouch.** Not silenced by a rule about who owns it -- contradicted by the evidence. The
  more it disagrees with what we have seen ourselves, the less its burn buys, and it can
  disagree its way to zero.
* **A proof we share no ground with scores zero, at any burn.** Sharing no ground is the
  default: a freshly minted proof that has only ever spoken about its own node overlaps
  with our opinions nowhere, so ``cred = 0`` and the second-proof trick buys nothing.
  Minting is free and burning is not, and neither is the thing being tested.
* **A newcomer can still earn a voice**, which a test of who owns the proof could never
  let it do. Agreeing with us about peers we both know is a track record that does not
  require us to have transacted with the proof's owner, or to know who the owner is.

**Known cost, and it is real: agreement can be mirrored.** This node publishes its own
local scores to the chain (``submit_to_ledger``), so an attacker can read them, mint a
proof, copy our opinions verbatim, and reach ``cred ~ 1`` for the price of the burn. That
is not fixable by dating the boxes -- revising a box spends it and rewrites its date
(see ``Opinion.published_at``) -- and it is why the *bounds* matter more than the
agreement score does. A perfect mirror is still capped at ``ONCHAIN_PUBLISHER_CAP`` of
one publisher's worth, the sum still saturates, and the whole term is still weighed below
``DONATION_WEIGHT`` by a validator that refuses any other order. The most that buys is
the term's ceiling, which is priced at less than 3 ERG donated. A mirror also has to keep
mirroring: it is agreeing with our verdicts, so it is publishing that the peers we
distrust are untrustworthy, which is not a free thing for a coalition to say.

Everything here is computed from rows already in SQLite -- a routing decision does no
network I/O at all. The chain is read on the periodic tick (:mod:`.onchain_indexer`), and
if that has never succeeded every candidate's standing is zero: all of them, never some,
because a half-filled index would silently favour whichever peers happen to be cached.
"""
from __future__ import annotations

from math import sqrt
from time import monotonic
from typing import Dict, Iterable, Mapping, Optional, Tuple

from src.balancers.scoring import DEFAULT_REPUTATION_HALF_CREDIT, reputation_factor
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

DEFAULT_ONCHAIN_WEIGHT = 0.1

#: Burn-weighted, agreed sum at which half the weight is earned, in ERG. Four publishers
#: at the cap, the same shape the donation term's half credit has.
DEFAULT_ONCHAIN_HALF_CREDIT = 20.0

#: The most one publishing proof may contribute to that sum, in ERG. Deliberately equal
#: to ``DONATION_HALF_CREDIT``: one proof, however funded and however much it agrees with
#: us, is worth at most what one median donation is.
DEFAULT_PUBLISHER_CAP = 5.0

NANOERG_PER_ERG = 10 ** 9

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


def agreement(
    verdicts: Mapping[str, float],
    local_scores: Mapping[str, float],
) -> float:
    """How much one proof sees the peers the way this node sees them, in ``[0, 1]``.

    The cosine of two vectors over the peers both have an opinion on: the proof's netted
    verdicts, and our own local standing for those same peers. Cosine rather than a raw
    dot product because the answer has to be about *direction* -- whether this proof
    praises who we praise and faults who we fault -- and not about how loudly either of
    us says it. Magnitude is what the burn and the cap are for, and letting it in twice
    would make a proof that stakes heavily on many peers look more credible for doing so.

    ``local_scores`` are the bounded factors ``r_hat``, not raw event sums, so one peer we
    have transacted with a thousand times cannot set the direction of our vector on its
    own.

    **Zero when the two overlap nowhere**, which is the common case and the intended one:
    a proof that has never spoken about a peer we know tells us nothing we can check, and
    an unverifiable claim is worth what an absent one is. Zero as the default is what
    makes a burn behind an unknown proof buy nothing at all.

    Zero, too, when they overlap but disagree outright. Clamped rather than
    sign-preserving: a negative cosine means *we do not listen*, not *we believe the
    opposite*. Were disagreement to invert, paying a proof to denounce a rival would
    promote that rival, and denouncing a peer we trust would become a way to have that
    peer promoted -- a weapon pointed at whoever the attacker chooses, bought at the
    price of a burn.
    """
    dot = norm_proof = norm_local = 0.0
    for subject, verdict in verdicts.items():
        ours = float(local_scores.get(subject) or 0.0)
        if not ours:
            # No opinion of our own on this peer, so nothing of the proof's to check
            # against. Left out of both norms as well as out of the dot product: an
            # unverifiable claim must not dilute the agreement it cannot inform.
            continue
        theirs = float(verdict or 0.0)
        dot += theirs * ours
        norm_proof += theirs * theirs
        norm_local += ours * ours
    if dot <= 0 or norm_proof <= 0 or norm_local <= 0:
        return 0.0
    return min(1.0, dot / sqrt(norm_proof * norm_local))


def contribution(
    verdict: float,
    burned_nanoerg: int,
    credibility: float,
    cap: float,
) -> float:
    """One proof's contribution to a subject's sum: burn-weighted, credible, capped.

    ``|verdict| * burned_erg`` is the sunk cost standing behind *this* opinion --
    ``Opinion.backed_nanoerg``, which multiplies the share rather than the raw token
    count, so minting a larger supply (free) buys nothing. That figure is then worth only
    what the proof saying it is worth to us, and no more than the cap either way.

    The cap applies to the magnitude and the sign survives it: a proof that stakes all of
    itself against a peer says something worth hearing, it just does not get to say it
    four times over however much it burned.
    """
    if credibility <= 0:
        return 0.0
    backed_erg = abs(float(verdict)) * (float(burned_nanoerg) / NANOERG_PER_ERG)
    magnitude = min(backed_erg, abs(float(cap)))
    return credibility * magnitude * (1.0 if verdict >= 0 else -1.0)


def standing(
    opinions: Iterable[Mapping[str, object]],
    *,
    local_scores: Mapping[str, float],
    excluded_proof_ids: Iterable[str] = (),
    cap: float = DEFAULT_PUBLISHER_CAP,
    half: float = DEFAULT_ONCHAIN_HALF_CREDIT,
) -> Dict[str, float]:
    """``o`` per subject peer. Pure: every input is passed in, nothing is read here.

    ``opinions`` are rows as :meth:`SQLConnection.get_onchain_opinions` returns them, one
    per (subject, publishing proof). They are read twice, and that is the point of
    keeping them in one table: grouped by proof they are each publisher's opinion vector,
    which is what :func:`agreement` scores; grouped by subject they are the verdicts on
    each candidate. The second pass costs one dictionary, not a second scan of the chain.

    ``excluded_proof_ids`` are the proofs this node publishes through. What they say is
    our own local scores restated (``submit_to_ledger``), so they agree with us perfectly
    by construction and would hand themselves the highest credibility in the table for
    it. Counting them would weigh one observation twice -- once as the local term nobody
    can buy, and once through the channel that is for sale.
    """
    excluded = {proof_id for proof_id in excluded_proof_ids if proof_id}

    rows = []
    by_publisher: Dict[str, Dict[str, float]] = {}
    for row in opinions:
        subject = str(row.get("subject_id") or "")
        proof_id = str(row.get("proof_id") or "")
        if not subject or not proof_id or proof_id in excluded:
            continue
        try:
            verdict = float(row.get("verdict") or 0.0)
            burned = int(row.get("burned_nanoerg") or 0)
        except (TypeError, ValueError):
            continue
        rows.append((subject, proof_id, verdict, burned))
        by_publisher.setdefault(proof_id, {})[subject] = verdict

    credibility = {
        proof_id: agreement(verdicts, local_scores)
        for proof_id, verdicts in by_publisher.items()
    }

    sums: Dict[str, float] = {}
    for subject, proof_id, verdict, burned in rows:
        value = contribution(verdict, burned, credibility.get(proof_id, 0.0), cap)
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

    Our own opinions are read **here**, not frozen into the index, so a peer that failed
    us an hour ago drags down its vouchers' credibility on the next routing decision
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

        sql = SQLConnection()
        rows = sql.get_onchain_opinions()
        if not rows:
            return {}

        # Our opinion of every peer we know, not only of the ones the chain mentions: a
        # proof is credible for agreeing with us about *any* peer we can check, and
        # restricting the comparison to the candidates in the table would score the same
        # proof differently depending on who else happened to be up for selection.
        half_local = _parameter("REPUTATION_HALF_CREDIT", DEFAULT_REPUTATION_HALF_CREDIT)
        local_scores = {
            peer_id: reputation_factor(compute_reputation(peer_id=peer_id), half_local)
            for peer_id in sql.get_peers_id()
        }

        standings = standing(
            rows,
            local_scores=local_scores,
            excluded_proof_ids=_own_proof_ids(),
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


def _own_proof_ids() -> Tuple[str, ...]:
    """The proofs this node publishes through, from the config.

    Ours and only ours. ``interface._own_proof_ids`` answers the same question for any
    node, but for a *peer* it answers it from the advertisement that peer signed, which
    is a claim rather than a fact. Here the answer is a local setting and cannot be
    wrong.
    """
    configured = str(
        ConfigManager().get("ledgers.ergo.reputation.REPUTATION_PROOF_ID") or ""
    )
    return (configured,) if configured else ()


def forget_cached_standings() -> None:
    """Drop the cached standings, so the next read recomputes them.

    For a caller that has just changed what the answer would be -- a fresh scan, or a
    test -- rather than for ordinary use, where the window expiring is the point.
    """
    global _cache
    _cache = None
