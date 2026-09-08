"""Energy price sources.

Samples persist energy and the tariff in effect at that moment; cost is
derived on read. A misconfigured or later-changed tariff therefore does not
rewrite history.

Only ``fixed`` is implemented. A day-ahead/spot API (REE/ESIOS, ENTSO-E) would
be another ``PriceSource``; do not half-wire it here.
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
