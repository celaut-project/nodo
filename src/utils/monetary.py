"""The node's unit of account, and how it is priced, settled and displayed.

Three separate things, deliberately not conflated:

**MU (monetary unit)** is what the node counts in. Prices, balances and charges are
integer MU, everywhere, so no amount ever goes through a float. MU has no intrinsic
value: it is the node's own accounting unit.

**What an MU is worth** is a property of each *payment contract*, and is deliberately
**not in this module**. A contract declares how many MU one of its units buys — that is
exactly what ``ContractRate.mu_per_unit`` carries on the wire, so a peer reading a price
can convert it to real money. Ergo's answer lives with Ergo, in
``src/payment_system/contracts/ergo/rate.py``; another ledger declares its own beside it.
Nothing here names a ledger, reads a ledger's config section, or knows a conversion into
real money, so a second payment system is added without touching the accounting core.

**What the operator reads and types** is a third thing again: the display unit
(``ui.DISPLAY_UNIT``). It exists so nobody has to think in MU. The units on offer are
whatever the configured payment contracts contribute (ERG, while Ergo is the only one),
plus ``mu`` and anything the operator declares by hand under ``ui.UNITS``.

Why this is not the gas model it replaced: gas had no declared rate anywhere, so a
price quoted in it meant nothing to the node reading it, and the shipped numbers put
charges and payments 56 orders of magnitude apart. Here the rate is explicit, travels
on the wire, and ``config_validation._warn_if_charges_cannot_settle`` checks at load
time -- against the raw config mapping, before this module's singleton is usable -- that
the two scales still meet.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Union

from src.utils.config import ConfigManager


def _config() -> ConfigManager:
    """The config, resolved per call rather than captured at import.

    ConfigManager is a singleton that can be replaced wholesale (tests do it, and a
    reload rebuilds it), so a module-level ``env_manager = ConfigManager()`` binds
    whichever instance existed when this module happened to be imported. This module is
    imported from nearly everywhere, so that would make prices depend on import order.
    The lookup is a dict hit.
    """
    return ConfigManager()


GIB = 1024 ** 3
HOUR_SECONDS = 3600

# Scarcity multipliers are fractional, and every charge must stay integer, so they are
# carried as basis points: 10_000 bp == 1.0x (no surcharge).
SCARCITY_SCALE = 10_000

# The one display unit that needs no rate, because it *is* the unit of account. Every
# other unit comes from a payment contract or from `ui.UNITS.<name>` (see `display_unit`).
UNIT_MU = "mu"


def _decimal(value, *, what: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{what} is not a number: {value!r}") from exc


def _number(key: str, default: float) -> float:
    raw = _config().get(key, default)
    return float(_decimal(raw, what=key))


# There is deliberately no MU-to-real-money conversion in this module. What an MU is
# worth belongs to the payment contract that settles it: see
# `src/payment_system/contracts/ergo/rate.py` for Ergo's. `mu_per_nanoerg`,
# `mu_to_nanoerg`, `nanoerg_to_mu` and `mu_per_erg` used to live here, which made the
# accounting core read one ledger's config section and hardcode its display unit.


# --------------------------------------------------------------------------------
# What the operator reads and types
# --------------------------------------------------------------------------------

@dataclass(frozen=True)
class DisplayUnit:
    """The unit the operator sees. Never used for accounting, only at the boundary."""

    name: str
    symbol: str
    mu_per_unit: Decimal
    decimals: int


def contract_display_units() -> Dict[str, Dict[str, Any]]:
    """Units the configured payment contracts offer, keyed by name.

    Imported lazily and only the contracts' light rate modules, because this is reached
    from ``format_mu``, which runs on log lines everywhere. A payment stack that will not
    import contributes nothing and money renders in raw MU; a rate that is present but
    malformed still raises, since that is a configuration error.
    """
    from src.payment_system.contracts.envs import display_units

    return display_units()


def default_display_unit_name() -> str:
    """What to show when ``ui.DISPLAY_UNIT`` is unset.

    The unit of whatever payment system this node is configured for -- ERG on a node
    settling in Ergo -- because that is the money the operator actually deals in. Raw MU
    only when no payment contract offers a unit at all, which is the honest answer: there
    is nothing to express the balance in.
    """
    return next(iter(contract_display_units()), UNIT_MU)


def display_unit() -> DisplayUnit:
    """The configured display unit (``ui.DISPLAY_UNIT``).

    Three sources, in order. ``mu`` needs no rate: it *is* the unit of account. Then the
    units the payment contracts contribute (``erg`` while Ergo is the only one), whose
    rates are derived from the ledger, so changing a ledger's rate cannot leave the
    displayed figure wrong. Then anything declared under ``ui.UNITS.<name>`` with
    ``MU_PER_UNIT`` (and optionally ``SYMBOL`` / ``DECIMALS``).

    A hand-declared unit whose real-world rate moves — a fiat one, say — is a static
    number and WILL go stale; nothing in the node refreshes it, and it never affects what
    is charged, only what is printed.
    """
    contributed = contract_display_units()
    fallback = next(iter(contributed), UNIT_MU)
    name = str(_config().get("ui.DISPLAY_UNIT", fallback) or fallback).strip().lower()

    if name == UNIT_MU:
        return DisplayUnit(name=UNIT_MU, symbol="MU", mu_per_unit=Decimal(1), decimals=0)

    # A contract-derived unit first, so an operator cannot shadow the ledger's own rate
    # with a stale hand-written one.
    declared: Dict[str, Any] = contributed.get(name) or _config().get(f"ui.UNITS.{name}", None) or {}
    if not declared:
        known = ", ".join(sorted([UNIT_MU, *contributed])) or UNIT_MU
        raise ValueError(
            f"ui.DISPLAY_UNIT is {name!r}, which is neither offered by a configured "
            f"payment contract ({known}) nor declared under ui.UNITS.{name}."
        )
    rate = _decimal(declared.get("MU_PER_UNIT", 0), what=f"ui.UNITS.{name}.MU_PER_UNIT")
    if rate <= 0:
        raise ValueError(f"ui.UNITS.{name}.MU_PER_UNIT must be positive, got {rate}.")
    return DisplayUnit(
        name=name,
        symbol=str(declared.get("SYMBOL", name.upper())),
        mu_per_unit=rate,
        decimals=int(declared.get("DECIMALS", 2)),
    )


def format_mu(amount_mu: int, *, with_symbol: bool = True) -> str:
    """Render an MU amount in the display unit. Display and logging only."""
    unit = display_unit()
    value = Decimal(int(amount_mu)) / unit.mu_per_unit

    if unit.decimals <= 0:
        text = format(value.to_integral_value(), "f")
    else:
        text = format(round(value, unit.decimals), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
    text = text or "0"
    return f"{text} {unit.symbol}" if with_symbol else text


def parse_to_mu(text: Union[str, int, Decimal]) -> int:
    """Parse an operator-supplied amount, in the display unit, into integer MU.

    Refuses anything that does not land on a whole MU rather than rounding it: the
    operator asked for a specific amount and must not silently be charged another.
    """
    unit = display_unit()
    value = _decimal(text, what=f"amount in {unit.symbol}")
    if value < 0:
        raise ValueError(f"Amount must not be negative: {text!r}")

    amount = value * unit.mu_per_unit
    if amount != amount.to_integral_value():
        raise ValueError(
            f"{text} {unit.symbol} is {amount} MU, which is not a whole number of MU. "
            f"The smallest amount this node can account for is {format_mu(1)}."
        )
    return int(amount)


# --------------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Prices:
    """This node's price vector, in MU.

    There is deliberately no single "price of compute": a node short on RAM but rich in
    disk has to be able to say so, which a scalar cannot express. Each dimension is
    priced on its own and scarcity is applied to each on its own.

    A price of 0 makes that dimension free.
    """

    # Recurring, charged for as long as an instance holds the resource.
    ram_mu_per_gib_hour: int
    cpu_mu_per_vcpu_hour: int
    disk_mu_per_gib_hour: int
    # Metered by volume rather than by time.
    net_mu_per_gib: int
    # One-off operations. Not scarcity-scaled: they price work done once, not occupancy.
    build_mu: int
    tunnel_open_mu: int
    modify_resources_mu: int
    # Scarcity surcharge: 1.0x when the resource is plentiful, up to this when it is
    # exhausted. The curve shapes how fast the surcharge arrives (1.0 = linear; higher
    # stays flat until the resource is genuinely scarce).
    scarcity_max_multiplier: int
    scarcity_curve: float


@dataclass(frozen=True)
class FreeTier:
    """What this node gives away.

    ``free_while_scarcity_below`` is a load threshold, not an amount: the node charges
    nothing while every resource sits below it, and prices normally once one does not.
    Combined with a price of 0 per resource, this covers the whole range an operator may
    want — expensive, cheap, free, or free up to a point.
    """

    credit_mu_per_new_client: int
    free_while_scarcity_below: float


def _price_mu(key: str) -> int:
    """Read a price. Prices are whole MU: the unit of account has no subdivision."""
    raw = _config().get(f"pricing.{key}", 0)
    value = _decimal(raw if raw not in (None, "") else 0, what=f"pricing.{key}")
    if value < 0:
        raise ValueError(f"pricing.{key} must not be negative, got {value}.")
    if value != value.to_integral_value():
        raise ValueError(
            f"pricing.{key} must be a whole number of MU, got {value}. Prices are in MU, "
            "the node's unit of account; there is nothing smaller to express."
        )
    return int(value)


def prices() -> Prices:
    """This node's current price vector.

    Read from config on every call rather than cached at import: ``ConfigManager``
    reloads the file when it changes on disk, so an operator can reprice a running node
    without restarting it.
    """
    max_multiplier = int(_number("pricing.SCARCITY_MAX_MULTIPLIER", 1))
    if max_multiplier < 1:
        raise ValueError(
            f"pricing.SCARCITY_MAX_MULTIPLIER must be at least 1 (1 = no surcharge), got {max_multiplier}."
        )
    curve = _number("pricing.SCARCITY_CURVE", 1.0)
    if curve <= 0:
        raise ValueError(f"pricing.SCARCITY_CURVE must be positive, got {curve}.")

    return Prices(
        ram_mu_per_gib_hour=_price_mu("RAM_MU_PER_GIB_HOUR"),
        cpu_mu_per_vcpu_hour=_price_mu("CPU_MU_PER_VCPU_HOUR"),
        disk_mu_per_gib_hour=_price_mu("DISK_MU_PER_GIB_HOUR"),
        net_mu_per_gib=_price_mu("NET_MU_PER_GIB"),
        build_mu=_price_mu("BUILD_MU"),
        tunnel_open_mu=_price_mu("TUNNEL_OPEN_MU"),
        modify_resources_mu=_price_mu("MODIFY_RESOURCES_MU"),
        scarcity_max_multiplier=max_multiplier,
        scarcity_curve=curve,
    )


def free_tier() -> FreeTier:
    raw_credit = _config().get("free_tier.CREDIT_MU_PER_NEW_CLIENT", 0)
    credit = _decimal(raw_credit if raw_credit not in (None, "") else 0,
                      what="free_tier.CREDIT_MU_PER_NEW_CLIENT")
    return FreeTier(
        credit_mu_per_new_client=int(credit),
        free_while_scarcity_below=_number("free_tier.FREE_WHILE_SCARCITY_BELOW", 0.0),
    )


# There is deliberately no settleability check here. Whether this node's prices and its
# payment rate still land on the same scale is asked once, at config load, by
# `config_validation._warn_if_charges_cannot_settle`, which reads the raw mapping. A
# second copy in this module was only ever called by its own tests, while the docstring
# claimed it ran at load time -- two implementations of one rule, one of them dead.


# --------------------------------------------------------------------------------
# Charge arithmetic
# --------------------------------------------------------------------------------

def per_time_charge(price_mu_per_unit_hour: int, units: Union[int, float, Decimal],
                    seconds: Union[int, float], scarcity_bp: int = SCARCITY_SCALE) -> int:
    """MU owed for holding ``units`` of a resource for ``seconds``.

    Integer throughout, so repeated ticks cannot drift. ``units`` is whatever the price
    is quoted per: GiB for memory and disk, vCPUs for compute.

    Truncation is toward zero, which can only lose a sub-MU remainder. Carrying that
    remainder across ticks is the exact fix and is deliberately not done: at any sane
    price a tick is worth thousands of MU, and the carry costs a database column. See
    docs/PRICING.md, "Rounding".
    """
    if price_mu_per_unit_hour <= 0 or units <= 0 or seconds <= 0:
        return 0
    # Scale before dividing so small quantities do not vanish.
    numerator = int(price_mu_per_unit_hour * Decimal(str(units)) * Decimal(str(seconds)) * scarcity_bp)
    return numerator // (HOUR_SECONDS * SCARCITY_SCALE)


def per_volume_charge(price_mu_per_gib: int, num_bytes: int, scarcity_bp: int = SCARCITY_SCALE) -> int:
    """MU owed for moving ``num_bytes``, priced per GiB."""
    if price_mu_per_gib <= 0 or num_bytes <= 0:
        return 0
    return (int(price_mu_per_gib) * int(num_bytes) * scarcity_bp) // (GIB * SCARCITY_SCALE)


def bytes_to_gib(num_bytes: int) -> Decimal:
    """Exact GiB for a byte count, for use as ``units`` in :func:`per_time_charge`."""
    return Decimal(int(num_bytes)) / Decimal(GIB)
