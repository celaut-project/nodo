"""The node's unit of account.

MU (monetary unit) is what the node counts in. It is always an integer, and it is
pegged:

    1 MU = 1 nanoERG = 1e-9 ERG

The peg is a definition, not a configuration key. It exists because rates are a *wire
contract*: ``mu_per_call`` and the rates in ``node_advertised_rates`` are read by other
nodes, and a peer can only act on a price if it knows what the unit is worth. The model
this replaced quoted costs in an undefined "gas", so an advertised rate carried no
information at all.

The peg does NOT tie the node to Ergo. It fixes the *scale* prices are expressed in,
not the currency they are settled in: a payment contract declares how many MU one of
its units is worth (celaut ``ContractRate``), and Ergo is simply the first one, at
``MU_PER_ERG``. A contract settling in another token declares its own rate.

Operators configure prices as decimal ERG strings. They are parsed exactly once, here,
into integer MU — the same discipline ``ergo_units`` already applies to wallet amounts,
for the same reason: every arithmetic step downstream is integer, so nothing drifts.

Nothing outside the node ever sees MU. The CLI renders ERG through
:func:`mu_to_erg_str`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Union

from src.utils.config import ConfigManager
from src.utils.ergo_units import NANOERG_PER_ERG, erg_to_nanoerg, nanoerg_to_erg_str


def _config() -> ConfigManager:
    """The config, resolved per call rather than captured at import.

    ConfigManager is a singleton that can be replaced wholesale (tests do it, and a
    reload rebuilds it), so a module-level ``env_manager = ConfigManager()`` binds
    whichever instance existed when this module happened to be imported. This module is
    imported from nearly everywhere, so that would make prices depend on import order.
    The lookup is a dict hit.
    """
    return ConfigManager()


# The peg. 1 MU == 1 nanoERG, by definition.
MU_PER_ERG = NANOERG_PER_ERG

GIB = 1024 ** 3
HOUR_SECONDS = 3600

# Scarcity multipliers are fractional, and every charge must stay integer, so they are
# carried as basis points: 10_000 bp == 1.0x (no surcharge).
SCARCITY_SCALE = 10_000


def erg_to_mu(value: Union[str, int, Decimal]) -> int:
    """Decimal ERG amount -> integer MU. Raises ``ValueError`` on anything unusable."""
    return erg_to_nanoerg(value)


def mu_to_erg_str(mu: int) -> str:
    """Human-readable ERG string for an MU amount. Display and logging only."""
    return nanoerg_to_erg_str(int(mu))


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


def _mu(key: str) -> int:
    """Read a price expressed in decimal ERG and return it in integer MU."""
    raw = _config().get(f"pricing.{key}", "0")
    try:
        return erg_to_mu(raw if raw not in (None, "") else "0")
    except ValueError as exc:
        raise ValueError(f"pricing.{key} is not a valid ERG amount: {raw!r} ({exc})") from exc


def _number(key: str, default: float) -> float:
    raw = _config().get(key, default)
    try:
        return float(Decimal(str(raw)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{key} is not a number: {raw!r}") from exc


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
        ram_mu_per_gib_hour=_mu("RAM_ERG_PER_GIB_HOUR"),
        cpu_mu_per_vcpu_hour=_mu("CPU_ERG_PER_VCPU_HOUR"),
        disk_mu_per_gib_hour=_mu("DISK_ERG_PER_GIB_HOUR"),
        net_mu_per_gib=_mu("NET_ERG_PER_GIB"),
        build_mu=_mu("BUILD_ERG"),
        tunnel_open_mu=_mu("TUNNEL_OPEN_ERG"),
        modify_resources_mu=_mu("MODIFY_RESOURCES_ERG"),
        scarcity_max_multiplier=max_multiplier,
        scarcity_curve=curve,
    )


def free_tier() -> FreeTier:
    raw_credit = _config().get("free_tier.CREDIT_ERG_PER_NEW_CLIENT", "0")
    try:
        credit = erg_to_mu(raw_credit if raw_credit not in (None, "") else "0")
    except ValueError as exc:
        raise ValueError(
            f"free_tier.CREDIT_ERG_PER_NEW_CLIENT is not a valid ERG amount: {raw_credit!r} ({exc})"
        ) from exc
    return FreeTier(
        credit_mu_per_new_client=credit,
        free_while_scarcity_below=_number("free_tier.FREE_WHILE_SCARCITY_BELOW", 0.0),
    )


def per_time_charge(price_mu_per_unit_hour: int, units: Union[int, float], seconds: Union[int, float],
                    scarcity_bp: int = SCARCITY_SCALE) -> int:
    """MU owed for holding ``units`` of a resource for ``seconds``.

    Integer throughout, so repeated ticks cannot drift. ``units`` is whatever the price
    is quoted per: GiB for memory and disk, vCPUs for compute.

    Truncation is toward zero, which can only lose a sub-MU remainder — a billionth of
    an ERG per tick. Carrying that remainder across ticks is the exact fix and is
    deliberately not done: at any sane price a tick is worth thousands of MU, and the
    carry costs a database column. See docs/PRICING.md, "Rounding".
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
