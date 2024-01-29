from typing import Optional

from src.utils.singleton import Singleton


class SimpleReputationFeedback(metaclass=Singleton):

    def __init__(self):
        self.counter = 0

    def submit_reputation_feedback(self, token: str, amount: int) -> Optional[str]:
        self.counter += 1
        print(self.counter)
        # Take the peer_id when the token it's external. Do nothing if it's an external service.
        if get_network_name(ip_or_uri=token.split('##')[1]) == DOCKER_NETWORK:
            pointer = token.split('##')[1]
            return lib_spend("", amount, pointer)

    def compute_reputation_feedback(self, pointer: str) -> float:
        self.counter += 1
        print(self.counter)
        return lib_compute(pointer, pointer)  # TODO The first param needs to be the root proof of the computation.


try:
    from reputation_graph import compute as lib_compute, spend as lib_spend

    from src.utils.env import DOCKER_NETWORK
    from src.utils.utils import get_network_name

    def submit_reputation_feedback(token: str, amount: int):
        return SimpleReputationFeedback().submit_reputation_feedback(token, amount)

    def compute_reputation_feedback(pointer):
        return SimpleReputationFeedback().compute_reputation_feedback(pointer)

except ModuleNotFoundError:
    def submit_reputation_feedback(token: str, amount: int):
        print("Not implemented")
        pass


    def compute_reputation_feedback(pointer):
        print("Not implemented")
        pass
