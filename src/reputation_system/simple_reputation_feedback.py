from typing import Optional

try:
    from reputation_graph import compute as lib_compute, spend as lib_spend

    from src.utils.env import DOCKER_NETWORK
    from src.utils.utils import get_network_name


    def submit_reputation_feedback(token: str, amount: int) -> Optional[str]:
        # Take the peer_id when the token it's external. Do nothing if it's an external service.
        if get_network_name(ip_or_uri=token.split('##')[1]) == DOCKER_NETWORK:
            pointer = token.split('##')[1]
            return lib_spend("", amount, pointer)


    def compute_reputation_feedback(pointer: str) -> float:  # TODO use it on balancers/estimated_cost_sorter
        return lib_compute(pointer, pointer)  # TODO The first param needs to be the root proof of the computation.

except ModuleNotFoundError:
    def submit_reputation_feedback(token: str, amount: int):
        pass


    def compute_reputation_feedback(pointer):
        pass
