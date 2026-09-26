"""Asking other nodes to resolve a communication domain.

The client half of ``Gateway.ResolveNetwork``. A ``Service.Network`` with no name to
look up -- a ``pow:<chain>`` one, say (issue #78) -- has to get its addresses from
somewhere, and other celaut nodes are the somewhere that already exists: they hold
the same kind of domain, they have already done the finding, and asking them costs
one round trip against a peer this node is in contact with anyway.

**Nothing that comes back is trusted, and nothing here is a shortcut past
verification.** A peer's answer is a list of addresses to *try*, ranked no higher
than the ones in this node's own ``config.yaml``: the caller puts each of them
through the same check it puts every other candidate through, and a peer that names
a hundred useless addresses has bought a hundred wasted HTTP requests and no firewall
rule. That is the property that makes it safe to ask strangers at all, and it is why
this module returns bare addresses rather than the ``Instance`` messages the peers
sent -- an ``Instance`` is the shape a *resolution* has, and calling a peer's
suggestion one would be recording the guess as an answer. What survives per address
is only the tags of the one slot it belonged to (e.g. ``pow_networks.REST_SLOT_TAG``)
-- which protocol that single port speaks, still verified by the caller and trusted no
more than the address itself -- never which other addresses the peer grouped it
with, since that grouping is the peer's belief about a third party and this node is
about to check each address on its own anyway.

Read-only and best-effort throughout. Every failure -- an unreachable peer, a
refusal, a malformed reply -- is one fewer source, never an exception: a launch that
aborted because a peer was down would have made the network a dependency of every
service that declares one.
"""
from __future__ import annotations

from typing import List, Optional, Set, Tuple

from bee_rpc import client as bee

from protos import celaut_pb2, celaut_pb2_grpc
from src.database.sql_connection import SQLConnection
from src.identity.grpc_transport import peer_channel
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger

env_manager = ConfigManager()
sc = SQLConnection()

#: How many peers are asked before this node settles for what it has. Asking is
#: sequential and each ask is a network round trip on the launch path, so this is a
#: latency budget rather than a quality setting: the answers are pooled, not compared,
#: so the tenth peer adds addresses nobody will get to before ``MAX_PEERS`` fills up.
CONFIG_BLOCK = "pow_networks"
ASK_PEERS_KEY = "ASK_PEERS"
DEFAULT_ASK_PEERS = 4

#: Addresses one peer may contribute. A reply is untrusted input that turns into
#: outbound requests, so its length is this node's decision, not the answering peer's.
MAX_ADDRESSES_PER_PEER = 32

#: One suggested address: its ip, its port, and the protocol tags of the one slot it
#: belonged to (empty if the answering slot declared none). A caller that does not
#: care about the tags can still unpack the first two and ignore the third.
SuggestedAddress = Tuple[str, int, Tuple[str, ...]]


def _ask_limit() -> int:
    """How many peers to ask, from config. Zero or less switches the source off."""
    try:
        return int(env_manager.get(f"{CONFIG_BLOCK}.{ASK_PEERS_KEY}", DEFAULT_ASK_PEERS))
    except (TypeError, ValueError):
        return DEFAULT_ASK_PEERS


def ask_peer(peer_id: str, network: celaut_pb2.Service.Network) -> List[SuggestedAddress]:
    """One peer's answer for ``network``, as ``(ip, port, tags)`` triples. ``[]`` on failure.

    Every uri of every instance is read, and the *instances* are flattened: which of a
    peer's addresses belong to the same remote node is the answering peer's belief
    about a third party, and this node is about to verify each address on its own
    anyway. Keeping that grouping would mean carrying a claim nothing here checks.

    The tags are not that kind of claim -- they name what a single, already-isolated
    port speaks (e.g. a ``pow:ergo`` peer tagging its REST slot apart from its P2P
    one, see ``pow_networks.resolve_pow_network``), the same fact a caller would
    otherwise have to guess at for that exact address. Read from the ``Api.Slot``
    whose ``port`` matches the uri's own ``Uri_Slot.internal_port``; a slot with no
    matching ``Api.Slot`` (or no ``protocol_stack`` at all) contributes no tags rather
    than failing the address.
    """
    try:
        resolution = next(bee.client_grpc(
            method=celaut_pb2_grpc.GatewayStub(
                peer_channel(peer_id=peer_id)
            ).ResolveNetwork,
            input=network,
            timeout=10,  # A non-answering peer must not hang service launch.
            indices_parser=celaut_pb2.ConfigurationFile.NetworkResolution,
            partitions_message_mode_parser=True
        ), None)
    except Exception as e:
        logger(f"[NETWORK-DISCOVERY] {peer_id} did not answer for {list(network.tags)}: "
               f"{type(e).__name__}: {e}")
        return []

    if resolution is None:
        return []

    addresses: List[SuggestedAddress] = []
    for instance in resolution.peer_instances:
        tags_by_port = {
            slot.port: tuple(
                tag for protocol in slot.protocol_stack for tag in protocol.tags
            )
            for slot in instance.api.slot
        }
        for uri_slot in instance.uri_slot:
            tags = tags_by_port.get(uri_slot.internal_port, ())
            for uri in uri_slot.uri:
                if len(addresses) >= MAX_ADDRESSES_PER_PEER:
                    logger(
                        f"[NETWORK-DISCOVERY] {peer_id} offered more than "
                        f"{MAX_ADDRESSES_PER_PEER} addresses for {list(network.tags)}; "
                        "the rest are ignored."
                    )
                    return addresses
                if uri.ip and uri.port:
                    addresses.append((str(uri.ip), int(uri.port), tags))
    return addresses


def ask_peers(
    network: celaut_pb2.Service.Network,
    limit: Optional[int] = None,
) -> List[SuggestedAddress]:
    """What the peers this node knows say about ``network``, de-duplicated, in ask order.

    Ask order and nothing more: the answers are pooled rather than voted on, because
    two peers naming the same address have not confirmed anything -- they may well have
    read it off the same list -- and treating agreement as evidence would be the one
    reading of this that *is* a trust decision. What decides is the verification the
    caller runs afterwards.

    De-duplicated by ``(ip, port)`` alone, keeping the tags of whichever peer named it
    first: the address is the same address whatever a later peer tags it, and ask
    order is the only ranking this function has an opinion about.
    """
    budget = _ask_limit() if limit is None else limit
    if budget <= 0:
        return []

    try:
        peer_ids = sc.get_peers_id()
    except Exception as e:
        logger(f"[NETWORK-DISCOVERY] could not list peers: {type(e).__name__}: {e}")
        return []

    seen: Set[Tuple[str, int]] = set()
    found: List[SuggestedAddress] = []
    for peer_id in peer_ids[:budget]:
        for ip, port, tags in ask_peer(peer_id, network):
            if (ip, port) in seen:
                continue
            seen.add((ip, port))
            found.append((ip, port, tags))

    if found:
        logger(
            f"[NETWORK-DISCOVERY] {len(found)} address(es) suggested for "
            f"{list(network.tags)} by up to {budget} peer(s)."
        )
    return found
