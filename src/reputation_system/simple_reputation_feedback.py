from typing import Optional

from src.utils.singleton import Singleton


class SimpleReputationFeedback(metaclass=Singleton):

    def __init__(self):
        proof = SimpleReputationFeedback.__static_submit_reputation_feedback("##", 1)
        print(f"Root proof: {proof}")
        if not proof:
            raise Exception("Invalid root proof")
        self.root_proof: str = proof if proof else ""

    @staticmethod
    def __static_submit_reputation_feedback(token: str, amount: int, root_proof: str = "") -> Optional[str]:
        # Take the peer_id when the token it's external. Do nothing if it's an external service.
        if get_network_name(ip_or_uri=token.split('##')[1]) != DOCKER_NETWORK:
            pointer: str = token.split('##')[1]
            return lib_spend(root_proof, amount, pointer)

    def submit_reputation_feedback(self, token: str, amount: int) -> Optional[str]:
        return SimpleReputationFeedback.__static_submit_reputation_feedback(token=token, amount=amount,
                                                                            root_proof=self.root_proof)

    def compute_reputation_feedback(self, pointer: str) -> float:
        _result = lib_compute(self.root_proof, pointer)
        print(f"Computed reputation: {_result}")
        return _result


try:
    from sigma_reputation_graph import compute as lib_compute, spend as lib_spend

    from src.utils.env import DOCKER_NETWORK
    from src.utils.utils import get_network_name


    def submit_reputation_feedback(token: str, amount: int):
        return SimpleReputationFeedback().submit_reputation_feedback(token, amount)

    def compute_reputation_feedback(pointer):
        return SimpleReputationFeedback().compute_reputation_feedback(pointer)

except ModuleNotFoundError:
    def submit_reputation_feedback(token: str, amount: int) -> str:
        print("Not implemented")
        return ""


    def compute_reputation_feedback(pointer) -> float:
        print("Not implemented")
        return 0
