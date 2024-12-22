from typing import Optional
from src.utils.env import EnvManager
from src.database.sql_connection import SQLConnection
from src.utils.logger import LOGGER
from src.reputation_system.contracts.ergo import submit_reputation_proof

sc = SQLConnection()
env_manager = EnvManager()

def update_reputation(token: str, amount: int) -> Optional[str]:
    # Take the peer_id when the token it's external. Do nothing if it's an external service.
    peer_id: str = token.split('##')[1] if "##" in token else token
    if sc.peer_exists(peer_id=peer_id):
        return sc.update_reputation_peer(peer_id, amount)

    # For services.
    # For clients.
    # For ledgers.

def compute_reputation(peer_id) -> float:
    """
    As an initial implementation, the node will only consider its own observations.
    Therefore, it will not take into account the reputation assigned by other peers for each of the pairs it interacts with.
    """
    _result: float = sc.get_reputation(peer_id)
    LOGGER(f"Computed reputation: {_result}")
    return _result

def submit_reputation(force_submit: bool = False):
    sc.submit_to_ledger(
        submit=lambda objects: submit_reputation_proof(objects=objects),
        force_submit=force_submit
    )
