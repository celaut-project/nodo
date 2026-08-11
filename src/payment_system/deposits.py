"""How large a deposit has to be, derived from what the ledger actually allows.

A deposit is not a number someone picks. Two hard floors decide it:

* a transaction costs a fee, and
* Ergo will not create an output below its minimum box value,

so the smallest payment that can exist at all is ``min_box + fee`` -- and at that size
the fee IS half the deposit. Sizing deposits by hand is how the old model ended up
refilling peers with an amount worth exactly one transaction fee.

Instead the operator states how much of a deposit may be lost to the fee
(``deposits.MAX_FEE_OVERHEAD``) and the amount follows from it.
"""
from __future__ import annotations

from src.utils.config import ConfigManager
from src.utils.monetary import format_mu, nanoerg_to_mu


def _ergo_floors() -> tuple[int, int]:
    """(fee, minimum box value), converted from nanoERG into MU.

    Both constants are Ergo's, so they are nanoERG; a deposit is MU. They are only the
    same number while MU_PER_NANOERG is 1, so the conversion is explicit.

    Imported lazily: the Ergo interface pulls in the payment stack, and deposit sizing is
    read from the manager loop.
    """
    from src.payment_system.contracts.ergo.interface import DEFAULT_FEE, SAFE_MIN_BOX_VALUE

    return nanoerg_to_mu(DEFAULT_FEE), nanoerg_to_mu(SAFE_MIN_BOX_VALUE)


def _share(key: str, default: float) -> float:
    # Resolved per call, not captured at import, for the same reason as
    # `monetary._config`: ConfigManager is a replaceable singleton, so a module-level
    # binding would make a deposit's size depend on import order. The lookup is a dict hit.
    value = float(ConfigManager().get(f"deposits.{key}", default))
    if not 0 < value <= 1:
        raise ValueError(f"deposits.{key} must be a share in (0, 1], got {value}.")
    return value


def full_deposit_mu() -> int:
    """The amount to top a peer up to.

    Large enough that the transaction fee stays under ``MAX_FEE_OVERHEAD`` of it, and
    never below what the ledger can actually settle.
    """
    fee, min_box = _ergo_floors()
    by_overhead = int(fee / _share("MAX_FEE_OVERHEAD", 0.02))
    return max(by_overhead, min_box + fee)


def refill_threshold_mu() -> int:
    """Balance on a peer below which it gets topped up again.

    A share of a full deposit rather than an independent constant, so the two cannot be
    configured into contradicting each other (a threshold above the deposit would refill
    on every single iteration).
    """
    return int(full_deposit_mu() * _share("REFILL_BELOW", 0.2))


def describe() -> str:
    """One line for logs and `nodo info`, in the display unit because a human reads it."""
    return (
        f"peer deposit {format_mu(full_deposit_mu())}, "
        f"refilled below {format_mu(refill_threshold_mu())}"
    )
