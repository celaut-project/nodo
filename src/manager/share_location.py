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
warned about with both locations, and the balancer chooses among the nodes that
hold it. Shares of one service that sit on different nodes, with no node holding
them all, still refuse the launch.

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


def placement(refs: List[ShareRef], holders: Dict[str, Set[str]]) -> List[str]:
    """The nodes a member of ``refs`` may run on; empty when no node holds any.

    A node qualifies when it holds every share of ``refs`` that is held anywhere.
    A share held on several nodes (a split) is no error: each of them is a
    candidate, and the balancer picks among them. Raises
    :class:`ShareLocationConflict` when the shares are on different nodes and no
    node holds them all, because one instance cannot run on two nodes.
    """
    held = [ref for ref in refs if holders.get(ref.share_id)]
    if not held:
        return []
    candidates = set.intersection(*(set(holders[ref.share_id]) for ref in held))
    if not candidates:
        lines = [
            f"{ref.describe()} on {', '.join(sorted(holders[ref.share_id]))}"
            for ref in held
        ]
        raise ShareLocationConflict(
            f"the network shares of this service are held on different nodes: "
            f"{'; '.join(lines)}. One instance cannot run on two nodes."
        )
    for ref in held:
        if len(holders[ref.share_id]) > 1:
            log.LOGGER(
                f"[SHARES] share {ref.describe()} exists on more than one node "
                f"({', '.join(sorted(holders[ref.share_id]))}): two members were placed "
                f"at the same time. The balancer chooses among "
                f"{', '.join(sorted(candidates))}; the copies are not merged."
            )
    return sorted(candidates)


def locate_network_shares(
    refs: List[ShareRef],
    ignore_network: Optional[str] = None,
) -> List[str]:
    """The nodes to run a member of ``refs`` on: ``LOCAL`` and/or peer ids.

    Empty means no node holds any of the shares yet, so the balancer places the
    member freely. With ``network.DELEGATE_EXECUTION`` off no peer is asked: this
    node does not delegate, so only what it holds itself can matter.
    """
    if not refs:
        return []
    peer_ids: List[str] = []
    if env_manager.get("network.DELEGATE_EXECUTION", True):
        from src.utils.utils import peers_id_iterator
        peer_ids = list(peers_id_iterator(ignore_network=ignore_network))
    where = placement(refs, share_holders(refs, peer_ids))
    log.LOGGER(
        f"[SHARES] {len(refs)} network share(s) "
        f"{'held on ' + ', '.join(where) if where else 'not held on any node yet'}."
    )
    return where
