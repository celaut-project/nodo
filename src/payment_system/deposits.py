"""How large a deposit has to be, derived from what the payment system actually allows.

A deposit is not a number someone picks. Two hard floors decide it, and both come from
the ledger rather than from configuration:

* a transaction costs a fee, and
* a ledger may refuse to create an output below some minimum,

so the smallest payment that can exist at all is ``minimum_output + fee`` -- and at that
size the fee is a large share of the deposit. Sizing deposits by hand is how the old model
ended up refilling peers with an amount worth exactly one transaction fee.

Instead the operator states how much of a deposit may be lost to the fee
(``deposits.MAX_FEE_OVERHEAD``) and the amount follows from it.

**No ledger is named in this module.** The floors are asked of each payment contract
through ``contracts.envs.settlement_floors``, the same dispatch the rest of the payment
flow uses, so adding a second payment system does not mean editing deposit sizing. This
used to import Ergo's ``DEFAULT_FEE`` and ``SAFE_MIN_BOX_VALUE`` directly, which imposed
Ergo's box-value floor on every contract -- including the simulated one, whose payments
never reach a chain.
"""
from __future__ import annotations

from typing import Tuple

from src.utils.config import ConfigManager


def _ledger_floors() -> Tuple[int, int]:
    """The strictest ``(fee, minimum output)`` across the available contracts, in MU.

    The strictest, because deposit sizing has no contract in hand: it produces one figure,
    used before anyone has chosen which contract will settle it. Taking the maximum makes
    that figure payable on every available system rather than only the cheapest. A contract
    with no fee and no minimum reports ``(0, 0)`` and so never raises the floor.

    ``settlement_floors`` either yields at least one contract or raises
    ``JavaDependencyMissing``, so there is no empty case to handle here. It can raise, which
    is why callers in the manager loop size deposits inside their guarded block.

    Imported lazily: the contract dispatch reaches the whole payment stack, and this is
    read from the manager loop.
    """
    from src.payment_system.contracts.envs import settlement_floors

    floors = [read() for read in settlement_floors().values()]
    return max(fee for fee, _ in floors), max(minimum for _, minimum in floors)


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
    fee, minimum_output = _ledger_floors()
    by_overhead = int(fee / _share("MAX_FEE_OVERHEAD", 0.02))
    return max(by_overhead, minimum_output + fee)


def refill_threshold_mu() -> int:
    """Balance on a peer below which it gets topped up again.

    A share of a full deposit rather than an independent constant, so the two cannot be
    configured into contradicting each other (a threshold above the deposit would refill
    on every single iteration).
    """
    return int(full_deposit_mu() * _share("REFILL_BELOW", 0.2))
