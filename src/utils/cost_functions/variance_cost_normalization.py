from src.utils.env import EnvManager

env_manager = EnvManager()
def variance_cost_normalization(cost: int, variance: float) -> int:
    return int(cost * (1 + variance * env_manager.get_env("COST_AVERAGE_VARIATION")))
