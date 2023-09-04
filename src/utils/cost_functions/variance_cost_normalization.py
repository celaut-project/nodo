from src.utils.env import COST_AVERAGE_VARIATION


def variance_cost_normalization(cost: int, variance: float) -> int:
    return int(cost * (1 + variance * COST_AVERAGE_VARIATION))
