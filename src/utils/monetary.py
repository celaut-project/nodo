"""The node's unit of account, and how it is priced, settled and displayed.

Three separate things, deliberately not conflated:

**MU (monetary unit)** is what the node counts in. Prices, balances and charges are
integer MU, everywhere, so no amount ever goes through a float. MU has no intrinsic
value: it is the node's own accounting unit.

**What an MU is worth** is a property of each *payment contract*, not of MU. A contract
declares how many MU one of its units buys — that is exactly what
``ContractRate.mu_per_unit`` carries on the wire, so a peer reading a price can convert
it to real money. Ergo is currently the only payment system, and
``ledgers.ergo.payments.MU_PER_NANOERG`` defaults to 1, which makes one MU one nanoERG.
Another ledger would declare its own rate.

**What the operator reads and types** is a third thing again: the display unit
(``ui.DISPLAY_UNIT``), ERG by default. It exists so nobody has to think in MU. A node
paid in something else, or an operator who would rather see a fiat figure, changes this
one key without touching a price.

Why this is not the gas model it replaced: gas had no declared rate anywhere, so a
price quoted in it meant nothing to the node reading it, and the shipped numbers put
charges and payments 56 orders of magnitude apart. Here the rate is explicit, travels
on the wire, and :func:`reference_charge_is_settleable` checks at load time that the two
scales still meet.

Note that ERG↔nanoERG (1e9) is NOT configurable and lives in ``ergo_units``: it is
fixed by the Ergo protocol, and making it a setting would only allow defining a wrong
Ergo.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Union

from src.utils.config import ConfigManager
from src.utils.ergo_units import NANOERG_PER_ERG


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

# Display units the node knows without being told. A custom one is declared under
# `ui.UNITS.<name>` (see `display_unit`), which is how a fiat display would be added.
UNIT_MU = "mu"
UNIT_ERG = "erg"


def _decimal(value, *, what: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{what} is not a number: {value!r}") from exc


def _number(key: str, default: float) -> float:
    raw = _config().get(key, default)
    return float(_decimal(raw, what=key))


# --------------------------------------------------------------------------------
# What an MU is worth, per payment system
# --------------------------------------------------------------------------------

def mu_per_nanoerg() -> Decimal:
    """How many MU one nanoERG buys.

    1 by default: Ergo is the only payment system, so the simplest mapping is the right
    one. It is configurable because the rate belongs to the *contract*, not to MU — a
    second ledger settling in another token declares its own.
    """
    raw = _config().get("ledgers.ergo.payments.MU_PER_NANOERG", 1)
    rate = _decimal(raw if raw not in (None, "") else 1,
                    what="ledgers.ergo.payments.MU_PER_NANOERG")
    if rate <= 0:
        raise ValueError(f"ledgers.ergo.payments.MU_PER_NANOERG must be positive, got {rate}.")
    return rate


def mu_per_erg() -> int:
    """MU bought by one whole ERG. This is what a peer is told as ``ContractRate``."""
    value = mu_per_nanoerg() * NANOERG_PER_ERG
    if value != value.to_integral_value():
        raise ValueError(
            f"ledgers.ergo.payments.MU_PER_NANOERG={mu_per_nanoerg()} makes one ERG "
            f"{value} MU, which is not a whole number of MU."
        )
    return int(value)


def mu_to_nanoerg(amount_mu: int) -> int:
    """MU -> nanoERG, for settling on Ergo. Truncates: never claim more than is owed."""
    return int(Decimal(int(amount_mu)) / mu_per_nanoerg())


def nanoerg_to_mu(nanoerg: int) -> int:
    """nanoERG -> MU, for crediting a payment that arrived."""
    return int(Decimal(int(nanoerg)) * mu_per_nanoerg())


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


def display_unit() -> DisplayUnit:
    """The configured display unit (``ui.DISPLAY_UNIT``), ERG by default.

    ``mu`` and ``erg`` are built in; ``erg`` derives its rate from the ledger, so
    changing ``MU_PER_NANOERG`` cannot leave the displayed figure wrong.

    Anything else must be declared under ``ui.UNITS.<name>`` with ``MU_PER_UNIT`` (and
    optionally ``SYMBOL`` / ``DECIMALS``). A unit whose real-world rate moves — a fiat
    one, say — is a static number here and WILL go stale; nothing in the node refreshes
    it, and it never affects what is charged, only what is printed.
    """
    name = str(_config().get("ui.DISPLAY_UNIT", UNIT_ERG) or UNIT_ERG).strip().lower()

    if name == UNIT_MU:
        return DisplayUnit(name=UNIT_MU, symbol="MU", mu_per_unit=Decimal(1), decimals=0)
    if name == UNIT_ERG:
        return DisplayUnit(
            name=UNIT_ERG, symbol="ERG", mu_per_unit=Decimal(mu_per_erg()), decimals=9
        )

    declared: Dict = _config().get(f"ui.UNITS.{name}", None) or {}
    if not declared:
        raise ValueError(
            f"ui.DISPLAY_UNIT is {name!r}, which is neither built in ('erg', 'mu') nor "
            f"declared under ui.UNITS.{name}."
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


def reference_charge_is_settleable() -> bool:
    """Do this node's prices and its payment rate still meet on the same scale?

    The failure this guards against is the one the gas model actually shipped with:
    charges of order 1e2 and a conversion factor of 1e58, so every real charge became
    zero on-chain and nothing could ever be settled. Splitting prices (MU) from the
    payment rate (MU per nanoERG) makes that reachable again by misconfiguration, so it
    is checked rather than assumed.

    The reference is an hour of one GiB of memory. A node that prices everything at 0 is
    deliberately free and passes.
    """
    reference_mu = prices().ram_mu_per_gib_hour
    if reference_mu <= 0:
        return True
    return mu_to_nanoerg(reference_mu) > 0


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
