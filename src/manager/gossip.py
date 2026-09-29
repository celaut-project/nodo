"""Bounded transitive peer discovery. Each manager-loop hook never raises.

Pull and push have independent clocks and flags, re-read every pass. Nothing is
forwarded in reaction to receipt; claims travel on independently scheduled ticks.
"""
import math
import random
import time
from itertools import chain, islice

from src.database.sql_connection import SQLConnection
from src.identity.grpc_transport import peer_channel
from src.utils.bee_client import BeeClient
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER
from src.utils.peer_gossip import gossip_limit, iter_gossip_peers, relayable_peer

DEFAULT_INTERVAL_SECONDS = 300
RPC_TIMEOUT_SECONDS = 10
_last_pull = None
_last_push = None


def _due(config, last):
    try:
        interval = float(config.get("communication.GOSSIP_INTERVAL_SECONDS",
                                    DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError, OverflowError):
        interval = DEFAULT_INTERVAL_SECONDS
    if not math.isfinite(interval) or interval <= 0:
        interval = DEFAULT_INTERVAL_SECONDS
    return last is None or time.monotonic() - last >= interval


def gossip_pull_tick():
    global _last_pull
    try:
        config = ConfigManager()
        if not config.get("communication.DISCOVER_PEERS_VIA_GOSSIP", True):
            return
        if not _due(config, _last_pull):
            return
        _last_pull = time.monotonic()
        limit = gossip_limit(config, "MAX_PEERS_PER_GOSSIP_RESPONSE", 100)
        if not limit:
            return
        sc = SQLConnection()
        ids = sc.get_peers_id()
        if not ids:
            return
        source = random.choice(ids)
        from src.manager.manager import add_peer_instance, get_client_id_on_other_peer

        client_id = get_client_id_on_other_peer(peer_id=source)
        if not client_id:
            return
        channel = peer_channel(peer_id=source)
        try:
            replies = BeeClient.list_peers(
                channel, client_id=client_id, timeout=RPC_TIMEOUT_SECONDS,
            )
            try:
                # Count *all* received messages, including rejected ones. A hostile
                # source does not get an unlimited stream by sending invalid peers.
                for peer in islice(replies, limit):
                    try:
                        if not relayable_peer(peer, getattr(peer, "public_key", "")):
                            continue
                        accepted = add_peer_instance(peer=peer)
                        LOGGER(f"[gossip] source={source} subject={peer.public_key} "
                               f"registration={'accepted' if accepted else 'refused'}")
                    except Exception as exc:
                        LOGGER(f"[gossip] rejected claim from source={source}: {exc}")
            finally:
                close = getattr(replies, "close", None)
                if close:
                    close()
        finally:
            channel.close()
    except Exception as exc:
        # Includes old peers returning UNIMPLEMENTED, bad config/DB and stream errors.
        LOGGER(f"[gossip] pull failed: {exc}")


def gossip_push_tick():
    global _last_push
    try:
        config = ConfigManager()
        if not config.get("communication.SHARE_KNOWN_PEERS", True):
            return
        if not _due(config, _last_push):
            return
        _last_push = time.monotonic()
        limit = gossip_limit(config, "MAX_PEERS_PER_GOSSIP_PUSH", 20)
        if not limit:
            return
        sc = SQLConnection()
        ids = sc.get_peers_id()
        if len(ids) < 2:
            return
        target = random.choice(ids)
        candidates = iter_gossip_peers(sc, limit, exclude_peer_id=target)
        first = next(candidates, None)
        if first is None:
            return
        from src.manager.manager import get_client_id_on_other_peer

        client_id = get_client_id_on_other_peer(peer_id=target)
        if not client_id:
            return
        channel = peer_channel(peer_id=target)
        try:
            # One total RPC budget, not 20 sequential ten-second stalls in billing.
            deadline = time.monotonic() + RPC_TIMEOUT_SECONDS
            for peer in chain((first,), candidates):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    result = BeeClient.introduce_peer(
                        channel, peer, client_id=client_id, timeout=remaining,
                    )
                    if result is None or result.token == "REFUSED":
                        LOGGER(f"[gossip] target={target} refused subject={peer.public_key}")
                except Exception as exc:
                    LOGGER(f"[gossip] push to target={target} "
                           f"subject={peer.public_key} failed: {exc}")
        finally:
            channel.close()
    except Exception as exc:
        LOGGER(f"[gossip] push failed: {exc}")
