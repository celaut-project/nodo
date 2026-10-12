"""Where a network share is: the node a member of it has to run on (#268).

A network share is a directory on one host. A member that starts on another node
would derive the same share id there and get a new, empty directory under it: two
shares with one name, and data that each half cannot see. So before a member is
placed, the node finds out which node already holds the share -- itself, from its
own share state, or a peer, over ``Gateway.LocateShare`` -- and the balancer runs
the member there. When no node holds it, the member is placed freely and the node
that runs it creates the share.

This is not consensus. Two members of a new share that are placed at the same
time, on two nodes that do not see each other's share yet, create two shares.
That split cannot be repaired after the fact without moving data, so the node
does not hide it: a later launch that finds the share on more than one node is
refused with both locations, and the operator decides which one to stop.

Peers that cannot be asked (unreachable, timed out, or running a version without
``LocateShare``) are skipped with a log line. They are taken as not holding the
share, which is the same position a peer that has never heard of it is in.
"""
from typing import Dict, List, Optional, Set

from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.shared_filesystems import ShareRef

LOCAL = "local"

env_manager = ConfigManager()


class ShareLocationConflict(Exception):
    """The network shares of one launch are held on more than one node.

    Either two of them are on different nodes, or one of them was created twice
    (a split). No placement keeps every share whole, so the launch is refused.
    """


def _timeout() -> Optional[int]:
    try:
        timeout = int(env_manager.get("EXTERNAL_COST_TIMEOUT"))
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def peer_holds_share(peer_id: str, share_id: str) -> Optional[bool]:
    """Ask one peer if it holds ``share_id``. ``None`` when it could not be asked."""
    # Imported here: they pull in gRPC and the database, which the pure placement
    # rule below does not need.
    from src.identity.grpc_transport import peer_channel
    from src.manager.manager import get_client_id_on_other_peer
    from src.utils.bee_client import BeeClient

    try:
        answer = BeeClient.locate_share(
            peer_channel(peer_id=peer_id),
            share_id,
            client_id=get_client_id_on_other_peer(peer_id=peer_id),
            timeout=_timeout(),
        )
    except Exception as e:  # noqa: BLE001 - a peer that cannot answer is skipped
        log.LOGGER(f"[SHARES] could not ask peer {peer_id} for share {share_id}: {e}")
        return None
    return bool(answer and answer.held)


def share_holders(
    refs: List[ShareRef],
    peer_ids: List[str],
    holds_locally=None,
    ask_peer=peer_holds_share,
) -> Dict[str, Set[str]]:
    """``share_id`` -> the nodes that hold it (``LOCAL`` for this one).

    Every share is asked of every peer, also a share this node holds: that is the
    only way to see a split.
    """
    if holds_locally is None:
        from src.virtualizers.microvm.shares import holds_network_share_locally
        holds_locally = holds_network_share_locally

    holders: Dict[str, Set[str]] = {}
    for ref in refs:
        found: Set[str] = set()
        if holds_locally(ref.share_id):
            found.add(LOCAL)
        for peer_id in peer_ids:
            if ask_peer(peer_id, ref.share_id):
                found.add(peer_id)
        holders[ref.share_id] = found
    return holders


def placement(refs: List[ShareRef], holders: Dict[str, Set[str]]) -> Optional[str]:
    """The node every share of ``refs`` is on, or ``None`` if no node holds any.

    Raises :class:`ShareLocationConflict` when they are on more than one node.
    """
    nodes: Set[str] = set()
    for ref in refs:
        nodes |= holders.get(ref.share_id, set())
    if len(nodes) <= 1:
        return next(iter(nodes), None)

    lines = []
    for ref in refs:
        where = sorted(holders.get(ref.share_id, set()))
        if where:
            lines.append(f"{ref.describe()} on {', '.join(where)}")
    split = [ref for ref in refs if len(holders.get(ref.share_id, set())) > 1]
    cause = (
        "a share was created on more than one node (two members were placed at the "
        "same time); stop the members on all but one of them"
        if split else
        "the service shares directories that are on different nodes, and one "
        "instance cannot run on two nodes"
    )
    raise ShareLocationConflict(
        f"the network shares of this service are held on more than one node: "
        f"{'; '.join(lines)}. {cause}."
    )


def locate_network_shares(
    refs: List[ShareRef],
    ignore_network: Optional[str] = None,
) -> Optional[str]:
    """The node to run a member of ``refs`` on: ``LOCAL``, a peer id, or ``None``.

    ``None`` means no node holds any of the shares yet, so the balancer places the
    member freely. With ``network.DELEGATE_EXECUTION`` off no peer is asked: this
    node does not delegate, so only what it holds itself can matter.
    """
    if not refs:
        return None
    peer_ids: List[str] = []
    if env_manager.get("network.DELEGATE_EXECUTION", True):
        from src.utils.utils import peers_id_iterator
        peer_ids = list(peers_id_iterator(ignore_network=ignore_network))
    where = placement(refs, share_holders(refs, peer_ids))
    log.LOGGER(
        f"[SHARES] {len(refs)} network share(s) "
        f"{'held on ' + where if where else 'not held on any node yet'}."
    )
    return where
