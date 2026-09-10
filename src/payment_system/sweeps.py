"""When to move a wallet's excess to cold storage. Pure arithmetic, any chain.

Shared rather than copied. Every payment system that holds a hot wallet wants the same
decision -- keep a working balance, move the rest, and never build an output the chain
will refuse -- and the numbers that differ are the chain's, not the rule's. A second
copy of this would be a second place for the retained-balance arithmetic to drift, on
the one path where drifting means sending money somewhere it cannot come back from.

All arithmetic is integer base units of one asset: nanoERG, satoshi, a token's smallest
unit. Nothing here knows which.
"""
from __future__ import annotations

from typing import Optional


def compute_sweep_amount(
    balance: int,
    hot_limit: int,
    min_transfer: int,
    fee: int,
    technical_min: int,
) -> Optional[int]:
    """The amount to move to cold storage, or ``None`` when nothing should move.

    ``excess = balance - hot_limit - fee``, swept only when it is at least the
    configured minimum transfer **and** at least the chain's smallest valid output. The
    hot limit, the fee and that technical minimum are always retained: a sweep that ate
    into any of them would leave the node unable to pay its next deposit, which is the
    one thing a hot wallet exists for.

    ``technical_min`` is the chain's floor on an output it will accept -- Ergo's minimum
    box value, Bitcoin's dust threshold. ``0`` for a system that has none.
    """
    excess = int(balance) - int(hot_limit) - int(fee)
    if excess < int(min_transfer):
        return None
    if excess < int(technical_min):
        return None
    return excess
