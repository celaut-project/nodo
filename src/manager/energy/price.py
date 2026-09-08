"""Energy price sources.

Samples persist energy and the tariff in effect at that moment; cost is
derived on read. A misconfigured or later-changed tariff therefore does not
rewrite history, and an hourly price needs no column of its own.

``fixed`` is the only source implemented, and the only one that has to work
offline. A day-ahead market source (REE/ESIOS, ENTSO-E) is another
``PriceSource`` and belongs beside this one, under three constraints that the
one-method interface hides:

- **``current()`` runs on the manager thread**, inside the sampling tick, next to
  ``maintain_vmachines`` and ``enforce_activity_window``. It must not do network
  I/O. Day-ahead prices make that easy rather than hard: the whole next day
  publishes at once, so a source fetches a curve on its own cadence and
  ``current()`` only indexes it by the hour.
- **A source is built once and kept** (see the registry in ``monitor``), so a
  cached curve survives between ticks. Anything rebuilt per sample would refetch
  every interval.
- **Offline is not a degraded mode, it is the floor.** A market source that cannot
  reach its API answers with the configured fixed tariff, never with a stale
  price and never with nothing.

And the market number is not the bill: a wholesale spot price carries no access
tolls, no electricity tax, no VAT and no retailer margin, which together are the
larger part of what an operator pays. A source that reports it raw understates
the cost with more decimal places than the guess it replaced, so the multiplier
and the fixed term belong in config next to the credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Tariff:
    price_per_kwh: float
    currency: str
    source: str


class PriceSource(Protocol):
    """Something that can name the price of a kWh right now.

    ``name`` is part of the contract, not decoration: it is what a sample stores
    in ``Tariff.source``, so a row says which source priced it.
    """

    name: str

    def current(self) -> Tariff:
        ...


class FixedPriceSource:
    """Config tariff. Works offline. ``price_per_kwh`` of 0 means "watts only"."""

    name = "fixed"

    def __init__(self, price_per_kwh: float, currency: str):
        try:
            price = float(price_per_kwh)
        except (TypeError, ValueError):
            price = 0.0
        if price < 0:
            price = 0.0
        self._price = price
        self._currency = (currency or "EUR").strip() or "EUR"

    def current(self) -> Tariff:
        return Tariff(
            price_per_kwh=self._price,
            currency=self._currency,
            source=self.name,
        )


JOULES_PER_KWH = 3.6e6


def energy_kwh(joules: float) -> float:
    return float(joules) / JOULES_PER_KWH


def cost_from_energy(joules: float, price_per_kwh: float) -> float:
    """Cost of a stored sample. Uses the sample's own tariff, never today's."""
    return energy_kwh(joules) * max(0.0, float(price_per_kwh))


def cost_per_hour(watts: float, price_per_kwh: float) -> float:
    """Present-tense hourly cost from a live watt reading and a tariff."""
    return (max(0.0, float(watts)) / 1000.0) * max(0.0, float(price_per_kwh))
