"""The funded life of an instance: how long the balance of a start pays (issue #344, 6.2).

The client does not say how long it needs the instance, and it may not know. The funded
life is the horizon that a balancer can always compute: the number of maintenance periods
that the balance pays, times ``maintenance_seconds_loop``.

It is a **lower bound** of what the client may need, because the client can add balance
later. The balance is ``initial_mu`` less the builds (``EstimatedCost.cost``), and the
periods are counted with the funding sum of the quote (``price_bound.funding_sum_mu``),
with the price bound of the quote if it has one. A start with no ``initial_mu`` gets the
default number of periods of this node.

The count stops at ``HORIZON_SECONDS``. The next close of a window is at most three days
away (``activity_window.next_close``), so a life beyond four days is "longer than any
close" and counting further would only cost time.
"""
import math
from typing import Dict, Optional

from src.utils.cost_functions import price_bound
from src.utils.cost_functions.start_funding import default_initial_periods
from src.utils.logger import LOGGER as logger
from src.utils.utils import from_amount

HORIZON_SECONDS = 4 * 24 * 3600


def funded_life_seconds(estimated_cost, initial_mu: Optional[int]) -> int:
    """The seconds that the balance of a start pays at ``estimated_cost``, at most
    ``HORIZON_SECONDS``. ``initial_mu`` is in the MU of the quote; None is a start with
    no ``initial_mu``, funded for the default periods of this node. 0 when the balance
    does not pay one period. A quote that cannot be read has no known life: it gets the
    horizon, so it is never ranked as "closes inside the life" by this figure alone."""
    try:
        loop = int(estimated_cost.maintenance_seconds_loop)
        if loop <= 0:
            return HORIZON_SECONDS
        if initial_mu is None:
            return min(default_initial_periods() * loop, HORIZON_SECONDS)
        balance = int(initial_mu) - int(from_amount(estimated_cost.cost))
        if balance <= 0:
            return 0
        init_mu = int(from_amount(estimated_cost.init_maintenance_cost))
        if init_mu <= 0:
            return HORIZON_SECONDS
        bound = price_bound.of_quote(estimated_cost)
        return _periods_paid(balance, init_mu, loop, bound) * loop
    except Exception as e:
        logger(f"Could not work out the funded life of a quote: {e}")
        return HORIZON_SECONDS


def _periods_paid(balance: int, init_mu: int, loop: int, bound) -> int:
    """The most periods ``i`` whose summed charge ``init_mu * (1 + v)^k(i)`` fits in
    ``balance``, counted up to the horizon."""
    most = math.ceil(HORIZON_SECONDS / loop)
    if bound is None or bound.volatility <= 0:
        return min(balance // init_mu, most)
    factors: Dict[int, Optional[float]] = {}
    total = 0.0
    for i in range(1, most + 1):
        k = price_bound.tick_period_index(bound, i, loop)
        if k not in factors:
            exact = bound.factor(k)
            factors[k] = None if exact is None else float(exact)
        factor = factors[k]
        if factor is None:
            return i - 1
        total += init_mu * factor
        if total > balance:
            return i - 1
    return most
