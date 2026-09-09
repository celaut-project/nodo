"""Donation credit: many donations on several chains, one bounded number per peer.

This is what makes donating worth making. A peer's credit is a *bonus* in the
balancer's score -- never a penalty, because penalising non-donors is what would make
patching donations out of a node rational -- and it is bounded, because nobody should
be able to buy their way past a peer that is cheaper or more reliable.

Everything here is computed from rows already in SQLite. A routing decision does no
network I/O at all: the chain is read on the periodic tick (:mod:`.indexer`), and if
that has never succeeded every candidate's credit is zero -- all of them, never some,
because a partial index would silently favour whichever peers happen to be cached.
"""
from __future__ import annotations

from decimal import Decimal
from math import log
from time import monotonic
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

# One year in seconds: the age at which a donation's weight has grown by ln(2) -- from
# 1.00 to 1.69. See `age_multiplier`.
DEFAULT_AGE_SCALE = 31536000
DEFAULT_HALF_CREDIT = Decimal("5000000000")

# How long a computed set of bonuses is reused. Short, because it also freezes the
# counted list read from the config, and an operator who has just added a wallet should
# see it take effect without restarting the node. Long enough that a burst of launches
# costs one pass over the index rather than one each.
CACHE_SECONDS = 60
_cache: Optional[Tuple[float, Dict[str, float]]] = None


def age_scale() -> Decimal:
    return _positive("balancers.DONATION_AGE_SCALE", DEFAULT_AGE_SCALE)


def half_credit() -> Decimal:
    return _positive("balancers.DONATION_HALF_CREDIT", DEFAULT_HALF_CREDIT)


def _positive(key: str, default) -> Decimal:
    """A positive parameter of the formula, or its default.

    Validated at load (``utils.config_validation.validate_balancers_config``), so a
    value that gets here malformed means the config changed under a running node. The
    default is used rather than raising: a routing decision must still complete.
    """
    raw = ConfigManager().get(key, default)
    try:
        value = Decimal(str(raw).strip())
    except Exception:
        value = Decimal(str(default))
    if not value.is_finite() or value <= 0:
        LOGGER(f"{key}={raw!r} is not positive; using {default}.")
        return Decimal(str(default))
    return value


def age_multiplier(age_seconds: float, scale: Decimal) -> Decimal:
    """``1 + ln(1 + age / scale)``: how much more an old donation is worth.

    Age *increases* the weight, which is the opposite of what a decay would do, and it
    is the point of the whole mechanism. Each node keeps its own list of whose
    contributions it recognises, so paying an old donation more makes "fund whoever you
    believe will be recognised later" a bet that pays off when that developer enters
    everybody else's list -- instead of everyone funding whoever already receives
    funding.

    A negative age (a row from above the height this node has indexed, which a reorg
    can produce) counts as brand new rather than as less than nothing.
    """
    if age_seconds <= 0:
        return Decimal(1)
    return Decimal(1) + Decimal(str(log(1 + float(age_seconds) / float(scale))))


def credit_by_peer(
    donations: Iterable[Mapping[str, Any]],
    *,
    weights: Mapping[str, Mapping[str, Decimal]],
    tips: Mapping[str, int],
    seconds_per_block: Mapping[str, int],
    to_mu: Mapping[str, Callable[[int, str], int]],
    scale: Optional[Decimal] = None,
) -> Dict[str, Decimal]:
    """Credit in MU per peer. Pure: every input is passed in, nothing is read here.

    ``weights`` are per ledger and already normalised to sum 1 within it -- see
    ``config.credit_weights``. Normalisation is load-bearing: unnormalised weights of
    100 would multiply every credit by a hundred, push every peer to the top of the
    saturation curve, and turn the donation term into a constant that ranks nobody
    above anybody.

    An address that is not in the ledger's list weighs nothing, so removing an address
    revokes the credit for every donation ever made to it, and adding one grants it --
    with its age multiplier already accrued. That retroactivity is deliberate: the
    chain did not change, this node's opinion did, and it is what makes betting on a
    developer's future recognition pay.
    """
    if scale is None:
        scale = Decimal(DEFAULT_AGE_SCALE)
    totals: Dict[str, Decimal] = {}
    for row in donations:
        peer_id = row.get("peer_id")
        ledger = str(row.get("ledger") or "")
        if not peer_id or not ledger:
            continue
        weight = weights.get(ledger, {}).get(str(row.get("to_address") or ""))
        if not weight or weight <= 0:
            continue
        convert = to_mu.get(ledger)
        if convert is None:
            # No rate for that chain here, so its donations have no comparable value.
            continue
        try:
            amount_mu = int(convert(int(row.get("amount_native") or 0), str(row.get("token_id") or "")))
        except Exception:
            continue
        if amount_mu <= 0:
            continue

        blocks = int(tips.get(ledger, 0)) - int(row.get("tx_height") or 0)
        age_seconds = max(0, blocks) * int(seconds_per_block.get(ledger, 0) or 0)
        contribution = Decimal(amount_mu) * weight * age_multiplier(age_seconds, scale)
        totals[peer_id] = totals.get(peer_id, Decimal(0)) + contribution
    return totals


def saturate(credit_mu: Decimal, half: Decimal) -> float:
    """``C / (C + C_half)`` in [0, 1): the share of the donation weight earned.

    Saturating by construction, so "donated an absurd amount" cannot run away with the
    ranking, and ``half`` is the only parameter -- the credit at which half the bonus is
    earned. No knowledge of the network's largest donor is needed, which is what a
    normalise-by-the-maximum shape would have required.
    """
    if credit_mu <= 0:
        return 0.0
    return float(credit_mu / (credit_mu + half))


def bonus_by_peer() -> Dict[str, float]:
    """``d̂`` per peer, read from SQLite. Never raises, never touches the network.

    Keyed as ``contract_instance.peer_id`` is, so **this node's own credit is under
    ``LOCAL_PEER_ID``**, not under whatever name a caller happens to use for itself.
    Our donations are read off the chain and counted with the same list as anybody
    else's -- our wallet is our identity, so it is the same code path with no special
    case -- and a caller that looks itself up by another name silently reads zero.

    An empty result means "no donation is recognised", which is also what a failed
    index looks like -- and that is the intended behaviour: every candidate gets zero,
    never some of them, so a half-filled index cannot tilt a routing decision.

    Cached for :data:`CACHE_SECONDS`, because this runs on the routing path and reads
    every donation the node has ever indexed. The index itself only changes when the
    hourly scan finds something, so recomputing it per launch is work nobody asked for
    -- and the rows never decay, so that work grows for ever. A failed computation is
    never cached: it must not blank every candidate's credit for a whole window.
    """
    global _cache
    cached = _cache
    if cached is not None and monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        from src.database.sql_connection import SQLConnection
        from src.payment_system.contracts import envs
        from src.payment_system.donations import config

        sql = SQLConnection()
        donations = sql.get_donations()
        if not donations:
            return {}

        ledgers = {str(row.get("ledger") or "") for row in donations}
        scanners = envs.donation_scanners()
        weights = {ledger: config.credit_weights(ledger) for ledger in ledgers}
        tips = {ledger: sql.donation_scan_tip(ledger) for ledger in ledgers}
        blocks = {ledger: envs.seconds_per_block(ledger) for ledger in ledgers}
        to_mu = {
            ledger: scanner.native_to_mu
            for ledger, scanner in scanners.items()
            if hasattr(scanner, "native_to_mu")
        }

        credits = credit_by_peer(
            donations,
            weights=weights,
            tips=tips,
            seconds_per_block=blocks,
            to_mu=to_mu,
            scale=age_scale(),
        )
        half = half_credit()
        bonuses = {peer_id: saturate(credit, half) for peer_id, credit in credits.items()}
        _cache = (monotonic(), bonuses)
        return bonuses
    except Exception as e:
        LOGGER(f"Could not compute donation credit; treating every candidate as zero: {e}")
        return {}


def forget_cached_bonuses() -> None:
    """Drop the cached credit, so the next read recomputes it.

    For a caller that has just changed what the answer would be -- a fresh index, or a
    test -- rather than for ordinary use, where the window expiring is the point.
    """
    global _cache
    _cache = None
