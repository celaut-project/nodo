from src.utils.env import EnvManager
from src.database.sql_connection import SQLConnection
from src.utils.logger import LOGGER
from src.reputation_system.contracts.ergo.transaction import submit_reputation_proof

sc = SQLConnection()
env_manager = EnvManager()

def update_peer_reputation(peer_id: str, amount: int) -> bool:
    """_summary_

    Args:
        peer_id (str): The ID of the peer whose reputation is to be updated
        amount (int): The amount to add to the reputation score.

    Returns:
        bool: True if the update was successful, False otherwise.
    """
    if sc.peer_exists(peer_id=peer_id):
        return sc.update_reputation_peer(peer_id, amount)

def update_container_reputation(container_id: str, amount: int) -> bool:
    """Update reputation of the peer where the container is been executed (if it's external) and the reputation of the container' service
    Args:
        container_id (str): Container's id
        amount (int): The amount to add to the reputation score.

    Returns:
        bool: True if the update was successful, False otherwise.
    """

    # TODO Add factors to allow different weights.

    if "##" in container_id:
        peer_id: str = container_id.split('##')[1]
        update_peer_reputation(peer_id=peer_id, amount=amount)
    
    # TODO update the service.

def compute_reputation(peer_id) -> float:
    # TODO Implement a TTL-based (Time-To-Live) caching mechanism.
    """
    As an initial implementation, the node will only consider its own observations.
    Therefore, it will not take into account the reputation assigned by other peers for each of the pairs it interacts with.
    """
    _result: float = sc.get_reputation(peer_id)
    return _result

def submit_reputation(force_submit: bool = False):
    sc.submit_to_ledger(
        submit=lambda objects: submit_reputation_proof(objects=objects),
        force_submit=force_submit
    )
