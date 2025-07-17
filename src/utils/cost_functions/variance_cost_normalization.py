from src.utils.config import ConfigManager

env_manager = ConfigManager()
def variance_cost_normalization(cost: int, variance: float) -> int:
    """
    Applies a normalization factor to a given cost based on its variance and a global environment setting.

    This function takes an initial cost and a variance value as input. It then calculates a normalized cost
    by adjusting the original cost based on the provided variance and a scaling factor retrieved from the
    environment variable "COST_AVERAGE_VARIATION". This allows for dynamic adjustment of costs based on
    their historical variability, potentially increasing costs with higher variance and decreasing them
    with lower variance relative to the average.

    Args:
        cost (int): The original cost value to be normalized.
        variance (float): A measure of the cost's variability (e.g., standard deviation squared).

    Returns:
        int: The normalized cost value, rounded to the nearest integer.

    """
    return int(cost * (1 + variance * env_manager.get_env("COST_AVERAGE_VARIATION")))
