"""How a donation debt becomes a set of outputs. Pure arithmetic, no chain, no config.

Everything here is integer base units of one asset -- nanoERG, or one base unit of a
token -- except the debt itself, which carries the fraction it accrued (see
``donation_accrual`` in ``src/database/migrate.py``).

Two rules decide the shape of a payout, and both exist because a chain charges a fee
and refuses an output below its minimum:

1. **The fee comes out of the debt, never on top of it.** The percentage an operator
   configures is what leaves the node, fee included; a fee charged on top would make
   the real cost of donating depend on how often the tick happens to fire, and could
   exceed the share the operator agreed to.
2. **A share that cannot go out stays owed *to that wallet*.** When a wallet's cut
   falls below the chain's minimum output -- or the wallet cannot be paid at all,
   because its address does not parse -- that cut is not redistributed among the
   others. It waits, and it waits *with its owner's name on it*.

   The ownership is the part that makes the promise true, and it took a second attempt
   to get right. The first version of this module recomputed every wallet's cut from
   whatever the debt happened to be at that moment, and put what it could not pay back
   into a debt that belonged to nobody. One tick later that money was split among
   everybody again -- so a wallet with a weight of 0.001 never cleared the floor, never
   got paid, and its share ended up in the big wallets after all. The docstring said
   otherwise and every test agreed with the docstring, because each test looked at a
   single call and the property is about a *sequence* of them.

   So a payout is computed from what each wallet is still owed, not from what the debt
   is now::

       entitlement(w) = weight(w) x (everything ever accrued) - (already paid to w)

   which grows tick after tick for a wallet that has not been paid, until it clears the
   floor and goes out in full. The weights then hold exactly rather than in the long
   run, and they hold for the unpayable address too: the operator's weights say who
   they meant to fund, so its share simply accumulates and is paid the moment the
   address is corrected -- which is what "corrected, it is paid what it was always
   owed" has to mean.

   Only whole native units move, so the sub-unit remainder stays on the debt row for
   the same reason.

3. **The threshold is what leaves the node, not what is owed.** ``DONATION_MIN_TRANSFER``
   says "never make a transfer smaller than this". Compared against the debt it would
   let a node with a 2 ERG minimum broadcast a transaction moving half of that -- the
   two figures differ by exactly what is being withheld -- and pay a full fee for it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import AbstractSet, List, Mapping, Optional, Sequence, Tuple

from src.payment_system.donations.config import Wallet


def _decimal(value) -> Decimal:
    """A stored decimal string read back, defaulting to zero.

    Zero is the reading that cannot make the node pay a wallet *less* than it is owed
    on the strength of an unreadable row, which is the direction this has to fail in.
    """
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return Decimal(0)
    return parsed if parsed.is_finite() and parsed > 0 else Decimal(0)


@dataclass(frozen=True)
class Payout:
    """One donation transaction, decided but not yet built."""

    outputs: List[Tuple[str, int]]
    fee_native: int
    #: What to add to each wallet's cumulative ``paid_native``: its output **plus the
    #: portion of the fee its own transfer consumed**. Not the same as ``outputs``, and
    #: the difference is load-bearing -- the debt is decremented by the outputs *and*
    #: the fee, so crediting only the outputs would leave every fee ever paid looking
    #: like it is still owed to somebody, and the entitlements would drift above the
    #: debt for ever. Summing this gives ``total_native`` exactly.
    credited: List[Tuple[str, int]]
    #: What this payout discharges: the outputs plus the fee. Exactly what the debt is
    #: decremented by once the transaction is on the wire.
    total_native: int
    #: What is left owed after it -- a share below the minimum output, plus the
    #: sub-unit fraction. Reported so a log line can say why the figures differ.
    withheld_native: Decimal


def normalised(wallets: Sequence[Wallet]) -> List[Wallet]:
    """The same wallets with weights summing to 1, in their declared order.

    An empty result for a list whose weights are all zero: that is a
    misconfiguration, reported at startup, and here it pays nobody rather than
    dividing by zero or splitting the money evenly among wallets the operator gave
    zero weight to.
    """
    total = sum((wallet.weight for wallet in wallets), Decimal(0))
    if total <= 0:
        return []
    return [Wallet(address=w.address, weight=w.weight / total) for w in wallets]


def entitlements(
    owed_native: Decimal,
    wallets: Sequence[Wallet],
    paid_native: Mapping[str, Decimal] = MappingProxyType({}),
) -> List[Tuple[str, int]]:
    """What each wallet is still owed, in declared order. The heart of the split.

    ``paid_native`` is what each wallet has already been credited, cumulatively, and it
    is what turns a weight from a per-transaction ratio into a share of everything this
    method has ever earned::

        accrued(current wallets) = owed + sum(paid to a wallet still in the list)
        entitlement(w)           = weight(w) x accrued - paid(w)

    The base counts only wallets still configured, which is what keeps the arithmetic
    closed: the entitlements then sum to exactly ``owed``, so a payout can never plan
    to move more than the debt, and a wallet the operator *removed* stops being funded
    without its history distorting everybody else's share.

    Two clamps, both for a weight lowered after a wallet was already paid:

    * a negative entitlement is zero -- there is no way to un-donate, and a wallet
      cannot be made to owe the node money;
    * and because zeroing those can push the rest above the debt, the total is trimmed
      back to it proportionally. Trimming rather than paying it out is the safe
      direction: the untrimmed amount is not owed yet, and it will be next tick.

    Only whole units, since only whole units can move. The fraction stays on the debt.
    """
    live = normalised(wallets)
    if not live:
        return []

    whole_debt = int(owed_native)
    already = {
        wallet.address: int(_decimal(paid_native.get(wallet.address, 0)))
        for wallet in live
    }
    accrued = whole_debt + sum(already.values())

    raw = [
        (wallet.address, int(Decimal(accrued) * wallet.weight) - already[wallet.address])
        for wallet in live
    ]
    clamped = [(address, max(0, amount)) for address, amount in raw]

    total = sum(amount for _, amount in clamped)
    if total > whole_debt > 0:
        # Proportional and integral, so the result is reproducible and never exceeds
        # the debt. What is trimmed stays owed and comes back on the next tick.
        clamped = [
            (address, amount * whole_debt // total) for address, amount in clamped
        ]
    elif whole_debt <= 0:
        return []
    return clamped


def _fee_shares(candidates: List[Tuple[str, int]], fee_native: int) -> List[int]:
    """How much of the fee each output bears: in proportion to what it carries.

    In proportion to the outputs *in this transaction*, never to the configured
    weights. A wallet that is not being paid this tick must not be charged for a
    transfer it was not part of -- charged by weight, a wallet too small to clear the
    floor would be billed for every transaction it missed and could end up owing more
    in fees than it was ever owed in donations.

    The remainder goes to the first output, in declared order, so the same inputs give
    the same transaction.
    """
    total = sum(amount for _, amount in candidates)
    if total <= 0 or fee_native <= 0:
        return [0] * len(candidates)
    shares = [fee_native * amount // total for _, amount in candidates]
    shares[0] += fee_native - sum(shares)
    return shares


def plan_payout(
    owed_native: Decimal,
    wallets: Sequence[Wallet],
    *,
    min_transfer_native: int,
    min_payable_native: int,
    fee_native: int,
    unpayable: AbstractSet[str] = frozenset(),
    paid_native: Mapping[str, Decimal] = MappingProxyType({}),
) -> Optional[Payout]:
    """Decide what to pay out of ``owed_native``, or ``None`` to keep waiting.

    ``min_payable_native`` and ``fee_native`` are the chain's own floors, in the
    asset's base units. They are passed in rather than read here: this function is
    shared by every payment system, and a chain's constants belong to that chain.

    ``paid_native`` is what each wallet has already been credited (see
    :func:`entitlements`); an empty mapping is a method that has never paid out.

    ``unpayable`` names wallets that must not receive an output on this round -- an
    address the chain will not accept, say. They are simply not paid, and their
    entitlement is untouched, so it grows and is paid in full once the address is
    corrected.

    Note that the debt and the floors are all native units. Comparing a native debt
    against a floor expressed in MU would be wrong by exactly the configured rate --
    which is invisible while ``MU_PER_NANOERG`` is 1, and silently wrong as soon as an
    operator changes it.
    """
    if owed_native is None or fee_native < 0:
        return None
    if int(owed_native) <= 0:  # the fraction can never be paid; it stays owed
        return None

    owing = entitlements(owed_native, wallets, paid_native)

    # Which wallets can actually be paid, and what the fee does to that. The two
    # questions are circular -- an output has to clear the minimum *after* bearing its
    # share of the fee, and the share depends on which outputs there are -- so the set
    # is narrowed until it stops changing. It shrinks every round, so this terminates
    # in at most one pass per wallet.
    candidates = [
        (address, amount) for address, amount in owing
        if amount >= min_payable_native and address not in unpayable
    ]
    while candidates:
        shares = _fee_shares(candidates, fee_native)
        payable = [
            (address, amount) for (address, amount), share in zip(candidates, shares)
            if amount - share >= min_payable_native
        ]
        if len(payable) == len(candidates):
            break
        candidates = payable

    if not candidates:
        return None

    shares = _fee_shares(candidates, fee_native)
    outputs = [
        (address, amount - share)
        for (address, amount), share in zip(candidates, shares)
    ]
    # What each wallet is credited is its whole entitlement: the output it receives plus
    # the fee its own transfer consumed.
    credited = list(candidates)
    total = sum(amount for _, amount in outputs) + fee_native

    # Against what leaves the node, not against what is owed. The two differ by exactly
    # what is being withheld, so comparing the debt would let a node configured never to
    # donate less than 2 ERG broadcast half of that -- and pay a whole fee to do it.
    if total < min_transfer_native:
        return None

    return Payout(
        outputs=outputs,
        fee_native=fee_native,
        credited=credited,
        total_native=total,
        withheld_native=owed_native - total,
    )
