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
2. **A share that cannot go out stays owed.** When a wallet's cut falls below the
   chain's minimum output -- or the wallet cannot be paid at all, because its address
   does not parse -- that cut is not redistributed among the others. It waits. A wallet
   with a small weight would otherwise never be paid at all: its cut would fall below
   the minimum every single time, be handed to the bigger wallets every single time,
   and its configured weight would be silently ignored. Waiting means the debt keeps
   growing until that wallet's share clears the floor, so the weights hold in the long
   run. This is the same reasoning that keeps the sub-unit remainder on the row.

   The unpayable-address case matters for the same reason and one more: the weights say
   who the operator meant to fund, so paying somebody else's share to the wallets that
   happen to parse would send money where it was not aimed. Held instead, it goes where
   it was meant to the moment the address is corrected.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import AbstractSet, List, Optional, Sequence, Tuple

from src.payment_system.donations.config import Wallet


@dataclass(frozen=True)
class Payout:
    """One donation transaction, decided but not yet built."""

    outputs: List[Tuple[str, int]]
    fee_native: int
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


def plan_payout(
    owed_native: Decimal,
    wallets: Sequence[Wallet],
    *,
    min_transfer_native: int,
    min_payable_native: int,
    fee_native: int,
    unpayable: AbstractSet[str] = frozenset(),
) -> Optional[Payout]:
    """Decide what to pay out of ``owed_native``, or ``None`` to keep waiting.

    ``min_payable_native`` and ``fee_native`` are the chain's own floors, in the
    asset's base units. They are passed in rather than read here: this function is
    shared by every payment system, and a chain's constants belong to that chain.

    ``unpayable`` names wallets that must not receive an output on this round -- an
    address the chain will not accept, say. They keep their weight for the split and
    simply are not paid, so their share stays owed rather than being handed to the
    wallets next to them in the list.

    Note that the debt and the floors are all native units. Comparing a native debt
    against a floor expressed in MU would be wrong by exactly the configured rate --
    which is invisible while ``MU_PER_NANOERG`` is 1, and silently wrong as soon as an
    operator changes it.
    """
    if owed_native is None or fee_native < 0:
        return None
    whole_debt = int(owed_native)  # the fraction can never be paid; it stays owed
    if whole_debt <= 0:
        return None

    distributable = whole_debt - fee_native
    if distributable <= 0:
        return None

    outputs: List[Tuple[str, int]] = []
    for wallet in normalised(wallets):
        share = int(Decimal(distributable) * wallet.weight)
        if share <= 0 or share < min_payable_native:
            continue
        if wallet.address in unpayable:
            continue
        outputs.append((wallet.address, share))

    if not outputs:
        return None

    required = max(min_transfer_native, len(outputs) * min_payable_native + fee_native)
    if whole_debt < required:
        return None

    total = sum(amount for _, amount in outputs) + fee_native
    return Payout(
        outputs=outputs,
        fee_native=fee_native,
        total_native=total,
        withheld_native=owed_native - total,
    )
