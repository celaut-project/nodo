from typing import Optional

from src.utils.config import ConfigManager
from src.database.sql_connection import SQLConnection
from src.reputation_system.opinions import NodeReputation, split_own
from src.utils.logger import LOGGER

sc = SQLConnection()
env_manager = ConfigManager()

def update_peer_reputation(peer_id: str, amount: int, reason: str) -> bool:
    """_summary_

    Args:
        peer_id (str): The ID of the peer whose reputation is to be updated
        amount (int): The amount to add to the reputation score.
        reason (str): Why, from `reasons.Reason`. Stored with the event, so a score
            can be read back as the things that caused it rather than one number.

    Returns:
        bool: True if the update was successful, False otherwise.
    """
    if sc.peer_exists(peer_id=peer_id):
        return sc.update_reputation_peer(peer_id, amount, reason)

def update_vmachine_reputation(vmachine_id: str, amount: int, reason: str) -> bool:
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

    What it does score is the *service* the vmachine runs, by `service_id`. That is the
    identity that survives the instance -- the instance is gone minutes later, while the
    service is what gets started again, and what a balancer could eventually weigh.

    Args:
        vmachine_id (str): Vmachine's id
        amount (int): The amount to add to the reputation score.
        reason (str): Why, from `reasons.Reason`.

    Returns:
        bool: True if the update was successful, False otherwise.
    """

    # TODO Add factors to allow different weights.

    try:
        service_id: str = sc.get_service_id_by_container_id(id=vmachine_id)
    except Exception as e:
        # The pruning paths call this while tearing an instance down, so losing the
        # race with its own deletion is ordinary. It costs one event, not a failure.
        LOGGER(f"No service to score for vmachine {vmachine_id}: {e}")
        return False

    if not service_id:
        return False

    return sc.update_reputation_service(service_id, amount, reason)

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


def get_node_reputation(node_id: Optional[str] = None) -> NodeReputation:
    """What the ledgers say about ``node_id``, defaulting to this node.

    The other direction from everything above it: :func:`compute_reputation` and the
    ``update_*`` functions are this node's own bookkeeping about *others*, kept locally
    and never published as such. This one reads the chain to answer the question an
    operator actually asks -- what has my node earned, in the eyes of the network -- and
    it is the only reputation figure here that other nodes can also see.

    A node is its identity public key (issue #236), which is what an opinion is
    addressed to (R5), so that is the argument: no wallet and no proof of our own is
    needed to ask, and the same call answers for a peer.

    One ledger exists, and a second would be another entry in the loop below with its
    own reader. A ledger that cannot be read lands in ``errors`` rather than raising:
    the answer from the ledgers that did respond is still worth having, and a page that
    goes blank because one explorer is down tells the operator less than one that says
    which explorer is down.
    """
    from src.identity.node_identity import get_node_public_key_hex

    node_id = node_id or (get_node_public_key_hex() or "")
    if not node_id:
        raise ValueError(
            "No node identity public key available (identity.MNEMONIC); there is no "
            "node for an opinion to be about."
        )

    collected = []
    errors = {}
    for ledger, reader in _opinion_readers().items():
        try:
            collected.extend(reader(node_id))
        except Exception as e:
            LOGGER(f"Could not read {ledger} reputation for {node_id}: {e}")
            errors[ledger] = str(e)

    own_proof_id = str(env_manager.get('ledgers.ergo.reputation.REPUTATION_PROOF_ID') or '')
    opinions, own = split_own(collected, own_proof_id)
    return NodeReputation(
        node_id=node_id,
        own_proof_id=own_proof_id,
        opinions=opinions,
        own=own,
        errors=errors,
    )


def _opinion_readers() -> dict:
    """Ledger tag -> the function that reads opinions off it.

    Imported here rather than at module scope because the Ergo reader pulls in
    ``requests`` and the contract constants, and this module is imported by the
    maintenance loop on every tick to move a peer's local score.
    """
    from src.reputation_system.contracts.ergo.opinions import opinions_about
    from src.reputation_system.envs import LEDGER

    return {LEDGER: opinions_about}
