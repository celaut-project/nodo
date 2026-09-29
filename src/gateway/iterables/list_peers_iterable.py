"""Bounded peer gossip, behind the same caller gate as GetPeerInfo."""
from protos import celaut_pb2
from src.database.sql_connection import SQLConnection
from src.gateway.client_gate import require_caller, simple_rpc_timeout_seconds
from src.utils.bee_client import BeeClient
from src.utils.config import ConfigManager
from src.utils.peer_gossip import gossip_limit, iter_gossip_peers


class ListPeersIterable:
    def __init__(self, request_iterator, context):
        self.request_iterator = request_iterator
        self.context = context

    def _peers(self):
        config = ConfigManager()
        if not config.get("communication.SHARE_KNOWN_PEERS", True):
            return
        limit = gossip_limit(config, "MAX_PEERS_PER_GOSSIP_RESPONSE", 100)
        for peer in iter_gossip_peers(SQLConnection(), limit):
            if not self.context.is_active():
                return
            yield peer

    def __iter__(self):
        client = BeeClient.parse_one(
            self.request_iterator, indices=celaut_pb2.Client,
            timeout=simple_rpc_timeout_seconds(),
        )
        require_caller(self.context, client.client_id if client else "")
        yield from BeeClient.respond(self._peers(), indices=celaut_pb2.Peer)
