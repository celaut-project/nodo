from typing import Optional
from src.utils.env import REPUTATION_DB

from src.utils.logger import LOGGER


try:
    from sigma_reputation_graph import compute as lib_compute, spend as lib_spend

    from src.utils.env import DOCKER_NETWORK
    from src.utils.utils import get_network_name


    def submit_reputation_feedback(token: str, amount: int) -> Optional[str]:
        # Take the peer_id when the token it's external. Do nothing if it's an external service.
        pointer: str = token.split('##')[1]
        if get_network_name(ip_or_uri=token.split('##')[1]) != DOCKER_NETWORK: 
            LOGGER(f"Submit reputation proof {pointer}") 
            return lib_spend("", amount, pointer, REPUTATION_DB)

    def compute_reputation_feedback(pointer) -> float:
        _result: float = lib_compute(None, pointer, REPUTATION_DB)
        LOGGER(f"Computed reputation: {_result}")
        return _result

except ModuleNotFoundError:
    def submit_reputation_feedback(token: str, amount: int) -> str:
        LOGGER("Not implemented")
        return ""


    def compute_reputation_feedback(pointer) -> float:
        LOGGER("Not implemented")
        return 0
