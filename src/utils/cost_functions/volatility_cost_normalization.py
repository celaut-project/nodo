import math

from src.utils.config import ConfigManager

env_manager = ConfigManager()


def volatility_cost_normalization(cost: int, volatility: float) -> int:
    """
    A cost inflated by the price volatility of its quote, to compare candidates (#636).

    ``cost * (1 + volatility * k)``, where ``k`` is ``balancers.COST_AVERAGE_VARIATION``:
    a quote whose price can rise faster is ranked as if it cost more. The setting lives
    under ``balancers:`` with the rest of the peer-selection formula: it weighs how the
    volatility of a quote counts when candidates are compared, which is selection rather
    than pricing.

    Never raises. A volatility or a factor that is NaN, infinite or negative is read
    strictly, as 0 (the cost as it is), and a product too large for an integer is the
    largest cost there is, so a candidate with it is ranked last instead of breaking the
    ranking (``int(inf)`` raises ``OverflowError``).

    Args:
        cost (int): The cost, in MU.
        volatility (float): ``EstimatedCost.price_volatility`` of a bounded quote, read
            strictly (``price_bound.strict``). 0 for an unbounded one.

    Returns:
        int: The normalized cost.
    """
    def _finite(value) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value >= 0 else 0.0

    factor = 1 + _finite(volatility) * _finite(env_manager.get("balancers.COST_AVERAGE_VARIATION", 1))
    try:
        value = float(cost) * factor
    except (TypeError, ValueError, OverflowError):
        return 0
    if math.isnan(value):
        return 0
    if math.isinf(value):
        return int(1e300) if value > 0 else 0
    return int(value)
