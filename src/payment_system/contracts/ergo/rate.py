"""What one MU is worth on Ergo, and the conversions that follow from it.

The node counts in MU, its own unit of account (``src/utils/monetary.py``). MU has no
intrinsic value: what one MU is *worth* is a property of each payment contract, and this
module is Ergo's answer. It is what travels to peers as ``ContractRate.mu_per_unit``, and
the single point where this ledger's money meets the node's accounting.

These functions used to live in ``monetary`` itself, which meant the generic money module
named a ledger, read that ledger's config section, and hardcoded its display unit — so
adding a second payment system meant editing the accounting core. Ergo's rate belongs
with Ergo.

Deliberately **light**: it imports config and pure arithmetic, nothing else. ``format_mu``
runs on log lines all over the node and resolves the display unit through here, so this
must not drag in the Ergo payment stack (``interface.py`` pulls in requests, the database
and the reputation system). ``interface.py`` imports *this*, never the reverse.

ERG <-> nanoERG (1e9) is not configurable and lives in ``src/utils/ergo_units.py``: it is
fixed by the Ergo protocol, and making it a setting would only allow defining a wrong Ergo.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict

from src.utils.config import ConfigManager
from src.utils.ergo_units import NANOERG_PER_ERG

# Config key holding this ledger's rate. Named here rather than in the accounting core,
# so a second ledger declares its own key without touching shared code.
RATE_KEY = "ledgers.ergo.payments.MU_PER_NANOERG"

# How this ledger's unit is shown to an operator who picks it as `ui.DISPLAY_UNIT`.
UNIT_NAME = "erg"
UNIT_SYMBOL = "ERG"
UNIT_DECIMALS = 9


def _decimal(value: Any, *, what: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{what} is not a number: {value!r}") from exc


def mu_per_nanoerg() -> Decimal:
    """How many MU one nanoERG buys.

    1 by default: Ergo is the only payment system, so the simplest mapping is the right
    one. It is a setting rather than a definition because the rate belongs to the
    contract — a second ledger settling in another token declares its own — and because
    an operator may want to rescale what an MU means against ERG.

    Read per call, not captured at import: ``ConfigManager`` reloads the file when it
    changes on disk, and it is a replaceable singleton.
    """
    raw = ConfigManager().get(RATE_KEY, 1)
    rate = _decimal(raw if raw not in (None, "") else 1, what=RATE_KEY)
    if rate <= 0:
        raise ValueError(f"{RATE_KEY} must be positive, got {rate}.")
    return rate


def mu_per_erg() -> int:
    """MU bought by one whole ERG. This is what a peer is told as ``ContractRate``."""
    rate = mu_per_nanoerg()
    value = rate * NANOERG_PER_ERG
    if value != value.to_integral_value():
        raise ValueError(
            f"{RATE_KEY}={rate} makes one ERG {value} MU, which is not a whole number of MU."
        )
    return int(value)


def mu_to_nanoerg(amount_mu: int) -> int:
    """MU -> nanoERG, for settling on Ergo. Truncates: never claim more than is owed."""
    return int(Decimal(int(amount_mu)) / mu_per_nanoerg())


def nanoerg_to_mu(nanoerg: int) -> int:
    """nanoERG -> MU, for crediting a payment that arrived."""
    return int(Decimal(int(nanoerg)) * mu_per_nanoerg())


def display_units() -> Dict[str, Dict[str, Any]]:
    """The display unit this contract contributes, keyed by name.

    Same shape as an operator-declared ``ui.UNITS.<name>`` block, so the accounting core
    handles both through one code path and knows nothing about ERG. The difference is that
    this rate is *derived* from the ledger, so it cannot go stale when the rate changes —
    a hand-declared unit is a static number nothing refreshes.
    """
    return {
        UNIT_NAME: {
            "SYMBOL": UNIT_SYMBOL,
            "MU_PER_UNIT": Decimal(mu_per_erg()),
            "DECIMALS": UNIT_DECIMALS,
        }
    }
