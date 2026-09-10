from src.utils.config import ConfigManager

env_manager = ConfigManager()
def variance_cost_normalization(cost: int, variance: float) -> int:
    """
    Applies a normalization factor to a given cost based on its variance and a global environment setting.

    This function takes an initial cost and a variance value as input. It then calculates a normalized cost
    by adjusting the original cost based on the provided variance and a scaling factor read from
    `balancers.COST_AVERAGE_VARIATION`. This allows for dynamic adjustment of costs based on
    their historical variability, potentially increasing costs with higher variance and decreasing them
    with lower variance relative to the average.

    The setting lives under `balancers:` with the rest of the peer-selection formula: it
    weighs how a quote's variability counts when candidates are compared, which is
    selection rather than pricing. It used to sit under `costs:` and is read by its
    explicit path here so the key has one unambiguous home.

    Args:
        cost (int): The original cost value to be normalized.
        variance (float): A measure of the cost's variability (e.g., standard deviation squared).

    Returns:
        int: The normalized cost value, rounded to the nearest integer.

    """
    return int(cost * (1 + variance * float(env_manager.get("balancers.COST_AVERAGE_VARIATION", 1))))
