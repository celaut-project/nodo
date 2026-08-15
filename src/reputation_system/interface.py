from src.utils.config import ConfigManager
from src.database.sql_connection import SQLConnection
from src.utils.logger import LOGGER

sc = SQLConnection()
env_manager = ConfigManager()

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

def update_vmachine_reputation(vmachine_id: str, amount: int) -> bool:
    """Score the service a vmachine runs. No peer is touched from here.

    This used to read a peer id out of the vmachine id (`id##peer_id`) and move that
    peer's score by the same amount. It was wrong twice over.

    It was wrong about *who*: every caller is `maintain.maintain_vmachines`, which
    iterates `get_all_internal_containers_ids` -- rows of `local_instances`, machines
    running on this node. So the `##` branch never fired, and the event was dropped
    rather than recorded anywhere. Nothing in the tree mints an id with `##` any
    more either; a delegated instance is keyed by the token the peer handed back, and
    the peer behind it is read from `delegated_instances.peer_id`, never by splitting
    a string (see `metrics.get_metrics`).

    And it was wrong about *what*: the outcomes that reach this function are an
    instance pruned for running out of balance and a machine the virtualizer lost.
    Neither is evidence about a peer. A peer is penalised where a peer actually
    failed us -- a refused payment, an unanswerable `GetPeerInfo` -- and those call
    `update_peer_reputation` directly.

    Args:
        vmachine_id (str): Vmachine's id
        amount (int): The amount to add to the reputation score.

    Returns:
        bool: True if the update was successful, False otherwise.
    """

    # TODO Add factors to allow different weights.

    # TODO update the service.

def compute_reputation(peer_id) -> float:
    # TODO Implement a TTL-based (Time-To-Live) caching mechanism.
    """
    As an initial implementation, the node will only consider its own observations.
    Therefore, it will not take into account the reputation assigned by other peers for each of the pairs it interacts with.
    """
    _result: float = sc.get_reputation(peer_id)
    return _result

def submit_reputation(force_submit: bool = False) -> bool:
    from src.reputation_system.contracts.ergo.transaction import submit_reputation_proof

    return sc.submit_to_ledger(
        submit=lambda objects: submit_reputation_proof(objects=objects),
        force_submit=force_submit
    )
