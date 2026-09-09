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

**No ledger is named in this module.** The floors are asked of the payment contract
that is going to settle, through ``contracts.envs.settlement_floors`` -- the same
dispatch the rest of the payment flow uses -- so adding a second payment system does not
mean editing deposit sizing. This used to import Ergo's ``DEFAULT_FEE`` and
``SAFE_MIN_BOX_VALUE`` directly, which imposed Ergo's box-value floor on every contract,
including the simulated one, whose payments never reach a chain.

**And they are asked per system, not collapsed across all of them.** Every figure here
takes the payment system it is sizing for. The alternative -- one global figure, the
strictest floor across every contract -- is what the code did until Bitcoin made it
visible, and it silently prices every deposit at the most expensive chain the node
happens to support (#340 §5).
"""
from __future__ import annotations

from typing import Optional, Tuple

from src.utils.config import ConfigManager


def _floors_for(payment_system) -> Tuple[int, int]:
    """``(fee, minimum output)`` in MU, for the system that will actually settle.

    Per system, and that is the whole point. This used to take the **maximum** across
    every available contract, because it produced one figure before anyone had chosen
    which contract would settle it -- so with a second payment system registered, every
    deposit inherited the strictest chain's fee and dust limit. Ergo's floors are around
    a thousandth of a cent and Bitcoin's are around a dollar, so that "safe" maximum
    inflates an Ergo deposit by roughly three orders of magnitude (#340 §5), and Basis
    (#265), whose floors are `(0, 1)`, would inherit Ergo's and stop being worth
    anything at all.

    ``payment_system`` may be ``None`` for a payment that settles on no chain at all --
    the simulated contract -- which imposes no floor because there is nothing to pay a
    fee to.

    Imported lazily: the contract dispatch reaches the whole payment stack, and this is
    read from the manager loop.
    """
    if payment_system is None:
        return 0, 0

    from src.payment_system.contracts.envs import settlement_floors

    read = settlement_floors().get(payment_system.key)
    if read is None:
        # The system was shared with the peer a moment ago and its contract is not
        # offered now -- a runtime that went away. Nothing can be sized against it.
        raise ValueError(
            f"no payment method is available for {payment_system.key}, so a deposit "
            "cannot be sized for it"
        )
    return read()


def _share(key: str, default: float, *, ledger_tag: Optional[str] = None,
           asset: str = "") -> float:
    # Resolved per call, not captured at import, for the same reason as
    # `monetary._config`: ConfigManager is a replaceable singleton, so a module-level
    # binding would make a deposit's size depend on import order. The lookup is a dict hit.
    #
    # A ledger may override it, because a share that is right for one chain is absurd on
    # another: 2 % fee overhead on Ergo is a deposit worth a fraction of a cent, while
    # on Bitcoin the same 2 % demands a deposit worth years of runtime up front (§5).
    config = ConfigManager()
    value = None
    # Per *method* first: on Ergo a token settling through the same contract as ERG
    # declares its own share, because what is right for a chain's native unit can be
    # absurd for a token priced orders of magnitude away from it.
    if ledger_tag and asset:
        for entry in config.get(f"ledgers.{ledger_tag}.payments.ASSETS") or []:
            if isinstance(entry, dict) and str(entry.get("TOKEN_ID") or "") == asset:
                value = entry.get(key)
                break
    if value in (None, "") and ledger_tag:
        value = config.get(f"ledgers.{ledger_tag}.payments.{key}")
    if value in (None, ""):
        value = config.get(f"deposits.{key}", default)
    value = float(value)
    if not 0 < value <= 1:
        where = f"ledgers.{ledger_tag}.payments.{key}" if ledger_tag else f"deposits.{key}"
        raise ValueError(f"{where} must be a share in (0, 1], got {value}.")
    return value


def full_deposit_mu(payment_system=None) -> int:
    """The amount to top a peer up to, sized for the system that will settle it.

    Large enough that the transaction fee stays under ``MAX_FEE_OVERHEAD`` of it, and
    never below what that ledger can actually settle.
    """
    fee, minimum_output = _floors_for(payment_system)
    ledger_tag = getattr(payment_system, "ledger_tag", None)
    asset = getattr(payment_system, "asset", "") or ""
    by_overhead = int(
        fee / _share("MAX_FEE_OVERHEAD", 0.02, ledger_tag=ledger_tag, asset=asset)
    )
    return max(by_overhead, minimum_output + fee)


def refill_threshold_mu(payment_system=None) -> int:
    """Balance on a peer below which it gets topped up again.

    A share of a full deposit rather than an independent constant, so the two cannot be
    configured into contradicting each other (a threshold above the deposit would refill
    on every single iteration).
    """
    ledger_tag = getattr(payment_system, "ledger_tag", None)
    asset = getattr(payment_system, "asset", "") or ""
    return int(
        full_deposit_mu(payment_system)
        * _share("REFILL_BELOW", 0.2, ledger_tag=ledger_tag, asset=asset)
    )
