"""The price bound of a quote (#636): ``price_bounded``, the volatility ``v`` and the period ``T``.

A bounded quote promises that the price of the instance rises by at most a factor
``(1 + v)`` per period ``T``, counted from the launch of the instance. In the period ``k``
(``k = floor((t - launch) / T) + 1``, and ``k = 1`` when ``T = 0``) the price is at most
``P * (1 + v)^k``, where ``P`` is the price at the launch. A fall of the price is not
limited.

The same arithmetic serves this node and its clients:

* the ticks of a local instance clamp the multiplier of each resource to the ceiling
  (``execution_cost.maintenance_charge_mu``);
* a client funds a start with ``EstimatedCost.cost`` plus ``funding_sum_mu`` for the
  periods that it chooses, and this node funds a start that carries no ``initial_mu``
  with the same sum for ``deposits.DEFAULT_INITIAL_PERIODS`` periods;
* this node checks the charges of a peer against the bound that the peer signed
  (``statement_check.maintenance_overcharge``).

A value that a peer sends is read strictly (``strict``): a volatility that is NaN,
infinite or negative is 0 (a fixed price), and a negative period is 0 (one period).
"""
import math
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Optional

from src.utils.logger import LOGGER as logger

# A factor above this is treated as no limit at all: `(1 + v)^k` for a large `v` or a
# long life grows past any amount of MU, and Decimal or Fraction arithmetic on it only
# costs time. 10^30 times a price is more MU than exists.
_LARGEST_FACTOR_LOG = math.log(10 ** 30)

# The default of a ``bound`` argument: read the bound of now (``current_bound``). None is
# a real value, no bound, so it cannot be the default.
CURRENT = object()


@dataclass(frozen=True)
class PriceBound:
    """``volatility`` is ``v`` (finite, at least 0). ``period_seconds`` is ``T`` (at least
    0; 0 is one period for the whole life)."""
    volatility: float
    period_seconds: int

    def period_index(self, elapsed_seconds: Optional[float]) -> int:
        """``k``, the period that an instance launched ``elapsed_seconds`` ago is in.

        1 for ``T = 0``, and for an unknown or negative time: the first period is the
        one that a launch starts in.
        """
        if self.period_seconds <= 0 or elapsed_seconds is None:
            return 1
        elapsed = float(elapsed_seconds)
        if not math.isfinite(elapsed) or elapsed < 0:
            return 1
        return int(elapsed // self.period_seconds) + 1

    def _log_factor(self, k: int) -> float:
        return int(k) * math.log1p(self.volatility)

    def factor(self, k: int) -> Optional[Fraction]:
        """``(1 + v)^k``, exactly, or None when it is too large to be a limit."""
        if self.volatility <= 0:
            return Fraction(1)
        if self._log_factor(k) > _LARGEST_FACTOR_LOG:
            return None
        return (1 + Fraction(Decimal(repr(self.volatility)))) ** int(k)

    def ceiling_bp(self, launch_bp: int, k: int, cap_bp: int) -> int:
        """The highest multiplier, in basis points, in the period ``k``: ``launch_bp``
        times ``(1 + v)^k``, and never more than ``cap_bp``
        (``SCARCITY_MAX_MULTIPLIER``)."""
        launch_bp, cap_bp = int(launch_bp), int(cap_bp)
        if launch_bp >= cap_bp:
            return cap_bp
        if launch_bp > 0 and self._log_factor(k) >= math.log(cap_bp / launch_bp):
            return cap_bp
        factor = self.factor(k)
        if factor is None:
            return cap_bp
        return min(cap_bp, int(launch_bp * factor))


def _rounded(value: float) -> float:
    """A float of a proto (``float`` is 32 bits) as the decimal that it was written as:
    0.1 arrives as 0.10000000149011612."""
    return float(f"{value:.7g}")


def strict(volatility, period_seconds, *, source: str = "") -> PriceBound:
    """The bound of a quote that this node did not make, read strictly (#636, F).

    A volatility that is not a number, not finite or negative is 0: the strictest
    reading, a price that does not rise. A negative or unreadable period is 0: one
    period for the whole life, so the price never rises above ``P * (1 + v)``. Each
    correction is logged.
    """
    try:
        v = float(volatility)
    except (TypeError, ValueError):
        v = float("nan")
    if not math.isfinite(v) or v < 0:
        logger(f"[PRICING] Invalid price_volatility {volatility!r}{source}; read as 0.")
        v = 0.0
    else:
        v = _rounded(v)
    try:
        t = int(period_seconds)
    except (TypeError, ValueError, OverflowError):
        t = -1
    if t < 0:
        logger(f"[PRICING] Invalid volatility_period_seconds {period_seconds!r}{source}; read as 0.")
        t = 0
    return PriceBound(v, t)


def of_quote(estimated_cost, *, source: str = "") -> Optional[PriceBound]:
    """The bound that ``estimated_cost`` promises, read strictly, or None when it
    promises none (``price_bounded`` false)."""
    if not getattr(estimated_cost, "price_bounded", False):
        return None
    return strict(estimated_cost.price_volatility, estimated_cost.volatility_period_seconds,
                  source=source)


def tick_period_index(bound: PriceBound, i: int, loop_seconds: int) -> int:
    """``k(i)``, the volatility period of the tick ``i`` (1 for the first tick).

    The tick ``i`` charges the maintenance period that ends ``i * loop_seconds`` after
    the launch, and the ticks take ``k`` from the time at the end of the interval that
    they charge (``execution_cost.maintenance_charge_mu``). So ``k(i)`` is
    ``period_index(i * loop_seconds)``: ``floor(i * loop_seconds / T) + 1``, and 1 when
    ``T = 0``. The funding sum (``funding_sum_mu``) uses this function, so a client, the
    default balance of this node and the ticks use the same ``k(i)`` (#636, 6).
    """
    return bound.period_index(int(i) * max(int(loop_seconds), 0))


def funding_sum_mu(init_mu: int, loop_seconds: int, bound: Optional[PriceBound],
                   periods: int) -> int:
    """The balance that funds ``periods`` maintenance periods at the start resources, in
    MU: what a client adds to ``EstimatedCost.cost`` (the comment of
    ``EstimatedCost.cost``).

    The sum, for each period ``i`` from 1 to ``periods``, of
    ``init_mu * (1 + v)^k(i)``, where ``k(i)`` is ``tick_period_index``. Rounded up to a
    whole MU. With no bound, the node promises nothing, and this is ``init_mu`` for each
    period, at the price of the estimate.

    This node uses it too: for the default balance of a start that carries no
    ``initial_mu`` (``deposits.DEFAULT_INITIAL_PERIODS`` periods) and for the least
    balance of a start (one period, ``start_funding.minimum_runtime_mu``).
    """
    init_mu, periods, loop_seconds = int(init_mu), int(periods), int(loop_seconds)
    if init_mu <= 0 or periods <= 0:
        return 0
    if bound is None or bound.volatility <= 0:
        return init_mu * periods
    total = Fraction(0)
    for i in range(1, periods + 1):
        k = tick_period_index(bound, i, loop_seconds)
        factor = bound.factor(k)
        if factor is None:
            raise OverflowError(f"(1 + {bound.volatility})^{k} is too large to fund")
        total += init_mu * factor
    return math.ceil(total)


def to_fields(bound: Optional[PriceBound]) -> dict:
    """The fields of ``EstimatedCost`` for ``bound``: none of them for no bound."""
    if bound is None:
        return {"price_bounded": False}
    return {
        "price_bounded": True,
        "price_volatility": float(bound.volatility),
        "volatility_period_seconds": int(bound.period_seconds),
    }


def current_bound() -> Optional[PriceBound]:
    """The bound that this node promises for a quote or a launch now, or None.

    None when ``pricing.PRICE_BOUNDED`` is false. The volatility is
    ``pricing.PRICE_VOLATILITY`` when the operator set it, or the one that the node
    computes from its own history of the price (``price_history.computed_volatility``).
    Read once per quote and once per launch: a launch stores what it read
    (``local_instances.launch_volatility``), and its ticks use that.
    """
    from src.utils.monetary import prices

    p = prices()
    if not p.price_bounded:
        return None
    if p.price_volatility is not None:
        return PriceBound(float(p.price_volatility), int(p.volatility_period_seconds))
    from src.utils.cost_functions.price_history import computed_volatility

    return PriceBound(
        computed_volatility(
            period_seconds=p.volatility_period_seconds,
            window_seconds=p.volatility_window_seconds,
            floor=p.volatility_min,
        ),
        int(p.volatility_period_seconds),
    )
