import socket, os, json
from typing import Dict, List, Optional
from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils.config import ConfigManager
from src.utils.registry_errors import ServiceNotInRegistry, ServiceSpecUnavailable
from src.utils.utils import load_service_from_disk
from src.manager.network_env import (
    PeerEnvLookup,
    filter_peers_by_environment,
)
from src.identity.node_identity import same_component
from src.utils.network_policy import enforce_network_policy
from src.manager.pow_networks import POW_TAG_PREFIX, resolve_pow_network

env_manager = ConfigManager()
sc = SQLConnection()

class NetworkAuthorizationError(Exception):
    """The networks an instance may use could not be derived from its ancestry.

    Raised instead of granting: the launch is aborted rather than continued with a
    set of networks nobody checked.
    """

def resolve_domain(domain: str) -> List[celaut.Instance.Uri]:
    """
    Resolve a domain to its associated IPv4 addresses.
    """
    try:
        ips = list({
            info[4][0]
            for info in socket.getaddrinfo(domain, None)
            if info[0] == socket.AF_INET
        })


        # TODO Must be based on the network client protocol stack. ¿?

        # Auxiliar, only http and https
        return [
            celaut.Instance.Uri(ip=ip, port=port)
            for ip in ips
            for port in [80, 443]  # Ports should be based on the protocol stack ¿?
        ]
    
    except socket.gaierror:
        raise ValueError(f"Cannot resolve domain: {domain}")

# Which ledger tags resolve to no peer instances at all, and why. A payment system is
# not a service this node can be pointed at: `bitcoin` is reached over its own node's
# JSON-RPC, configured under `ledgers.bitcoin`, and there is no URI to advertise for it.
# Named here rather than left to fall through the loop below, so the answer is a
# statement instead of an accident.
LEDGERS_WITHOUT_URIS = ("bitcoin",)


def resolve_ergo_network() -> List[celaut.Instance.Uri]:
    return []

    # TODO Needs to get the ip and port from the data, actually is the restApiUrl.
    try:
        http_peers_file = env_manager.get("ledgers.ergo.HTTP_PEERS_PATH")
        if os.path.exists(http_peers_file):         
            with open(http_peers_file, 'r') as f:
                ergo_peers = json.load(f)

            result=[]
            for uri in ergo_peers.keys():
                ip, port = uri.split(":")
                result.append(celaut.Instance.Uri(ip=ip, port=int(port)))
            
            return result
    except:
        return []

def resolve_network(
    network: celaut.Service.Network,
    requester_env_values: Optional[Dict[str, bytes]] = None,
    peer_env_lookup: Optional[PeerEnvLookup] = None,
    ask_peers: bool = True,
) -> List[celaut.Instance]:
    """Peer instances for one declared communication domain.

    ``ask_peers=False`` forbids the resolution from asking other celaut nodes, and is
    passed by ``Gateway.ResolveNetwork`` when answering one. It is what keeps a question
    from being relayed: see :func:`pow_networks.candidate_urls`.
    """
    # Wildcard "*" (open-internet egress) and any unresolved tag resolve to no
    # concrete peer instances; initialise uris so an unmatched loop cannot raise
    # UnboundLocalError (it previously did for tag "*").
    uris: List[celaut.Instance.Uri] = []
    for tag in network.tags:
        # A `pow:<chain>` tag names a PoW communication domain, whose actual ask
        # lives in `Network.formal` (issue #78,
        # docs/proposals/78-network-guarantees-and-pow.md). It is dispatched first
        # and returns whole Instances rather than bare uris, because which peers
        # qualify is decided by verifying each candidate's chain state -- not by a
        # name lookup. The prefix carries no `.`, so such a tag could never have
        # reached the DNS heuristic below anyway; the order is for clarity.
        if tag.startswith(POW_TAG_PREFIX):
            peers = resolve_pow_network(network, tag=tag, ask_peers=ask_peers)
            if peers:
                return filter_peers_by_environment(
                    network=network,
                    peers=peers,
                    requester_env_values=requester_env_values,
                    peer_env_lookup=peer_env_lookup,
                )
            continue

        if tag in LEDGERS_WITHOUT_URIS:
            continue

        if "ergo" in tag:
            uris = resolve_ergo_network()
            if uris:
                break

        if not tag.islower() or '.' not in tag:
            continue

        uris = resolve_domain(tag)
        if uris:
            break

    if not uris:
        # No concrete peer URIs (e.g. wildcard "*"): the tag is honoured at the
        # firewall layer (allow-all egress); there is no peer instance to advertise.
        return []

    client_protocol_stack = network.protocol_stack
    i_slot = 1  # Default slot id (because internal port usage is irrelevant here)

    instance = celaut.Instance(
        api=celaut.Service.Api(
            slot=[celaut.Service.Api.Slot(
                port=i_slot,
                transport=celaut.Service.Api.Protocol(tags=["tcp"]),
                protocol_stack=client_protocol_stack
            )],
            payment_contracts=[]
        ),
        uri_slot=[celaut.Instance.Uri_Slot(
            internal_port = i_slot,
            uri=uris
        )]
    )

    return filter_peers_by_environment(
        network=network,
        peers=[instance],
        requester_env_values=requester_env_values,
        peer_env_lookup=peer_env_lookup,
    )

def resolve_network_for_peer(
    network: celaut.Service.Network,
    subject: str = "",
) -> celaut.ConfigurationFile.NetworkResolution:
    """Answer another node's ``Gateway.ResolveNetwork`` question about ``network``.

    The decisions behind that RPC, kept out of its gRPC plumbing so they can be read
    and tested as decisions. Two of them:

    * **The operator's policy applies.** ``service_networks`` says which domains this
      node will reach on anyone's behalf. Resolving one it refuses to reach -- handing
      over the addresses it would not use itself -- is reaching it by proxy, the same
      argument that puts the check before the balancer in ``launch_service`` rather
      than after it. The rejection propagates as a rejection, so the caller can tell
      "not from this node" from "nobody is there".
    * **The question is never relayed** (``ask_peers=False``). Answering it by asking
      our own peers, who ask theirs, is a walk over a graph nobody has a view of -- and
      two nodes that know each other are already a cycle. Each node answers from what
      it knows, and a caller wanting more breadth asks more nodes itself, which keeps
      the cost with whoever chose to spend it.

    No environment filter either: ``Network.environment_variable`` picks among *this*
    node's own instances by the requesting instance's value, and a remote caller is not
    one of them.
    """
    enforce_network_policy(networks=[network], subject=subject)
    return celaut.ConfigurationFile.NetworkResolution(
        tags=list(network.tags),
        peer_instances=resolve_network(network, ask_peers=False),
    )


def match_networks(a: celaut.Service.Network, b: celaut.Service.Network) -> bool:
    """Whether two ``Service.Network`` declarations name the same communication domain.

    A ``Network`` is a tags/prose/formal descriptor like every other replaceable
    component in celaut, so it is compared by the rule every other one is compared by
    (``node_identity.same_component``, and see ``Peer.SignatureScheme`` in
    celaut.proto): **formal decides whenever both sides declare one**, and otherwise
    one shared tag is enough.

    Reading ``formal`` is the point. Until now this was a tag intersection with a
    ``# TODO Could be more powerfull`` on it, which was harmless while nothing put
    anything in ``formal`` -- and stopped being harmless the moment a ``pow:ergo``
    network started carrying its actual ask there (issue #78). Two services both
    tagged ``pow:ergo`` asking for different blocks and different amounts of work are
    not in the same domain, and the ancestor chain was authorizing one as the other.

    What that costs, stated plainly, because it is a real narrowing of
    :func:`filter_networks_with_ancestors`: a parent that declares a ``formal`` grants
    its descendants **that** ask and no other, byte for byte, since two formals either
    are the same bytes or are not. A parent meaning to grant a family of asks -- any
    ``pow:ergo`` network its children care to specify -- says so by declaring the tag
    and leaving ``formal`` empty, which is the same thing it already meant. Nothing
    that declares no ``formal`` anywhere changes behaviour at all.

    Not ``same_component_stack``: that pairs up a *stack* of descriptors, and a
    Network is one descriptor. ``protocol_stack`` is not compared here either -- it
    says what the peers are expected to speak, not which domain this is, and a parent
    and child that name the same domain in different protocol terms are still naming
    the same domain.
    """
    return same_component(a, b)

def filter_networks_with_ancestors(networks: List[celaut.Service.Network], father_id: str) -> List[celaut.Service.Network]:
    """Keep only the networks that every ancestor of ``father_id`` also declares.

    This is the authorization control for ``Service.Network``: a network is usable
    only if every ancestor declares one that :func:`match_networks` accepts -- which
    reads ``formal`` when both sides carry one, and falls back to a shared tag. The AND over the chain is
    "only the direct father authorizes" applied by induction -- a father can only
    grant the domain its own father granted it, recursively -- so the walk
    re-derives the effective grant from each ancestor's spec. (It re-derives it
    because what is read is the ancestor's *spec*, i.e. what it asked for, not a
    persisted record of what it was actually granted.)

    An ancestor's spec that cannot be read raises and aborts the launch, because a
    spec the node failed to load is not a spec that declared no restrictions. Both
    ways of failing to read one end that way, each on its own grounds: an
    unloadable spec may well load on a retry, and a spec missing for a service
    this node launched is an inconsistent registry, since the launch path stores
    every spec it runs before running it. Telling the two apart is why the spec
    comes from ``load_service_from_disk`` and not from ``read_service_from_disk``,
    whose ``None`` covers both. Answering that ``None`` by handing the caller its
    own list back skipped this generation's check *and* every ancestor above it --
    the return preceded the recursion -- and its transient half, a timeout waiting
    to unlock memory, made the control degrade to allow-all exactly when the node
    was loaded enough to be pushed there (#269).
    """
    # Nothing left to authorize. No ancestor can subtract from an empty grant, so
    # the walk stops instead of reading specs whose answer cannot matter -- which
    # also keeps a launch that asks for no network at all from depending on the
    # registry being readable.
    if not networks:
        return []

    filtered = []
    service_id = sc.get_service_id_by_container_id(id=father_id)

    try:
        spec = load_service_from_disk(service_hash=service_id)
    except ServiceSpecUnavailable as e:
        raise NetworkAuthorizationError(
            f"Cannot authorize networks for {father_id}: the spec of its service "
            f"{service_id} is on the registry but was not loadable ({e}). Nothing is "
            "granted; the launch has to be retried when the node is less loaded."
        ) from e
    except ServiceNotInRegistry as e:
        raise NetworkAuthorizationError(
            f"Cannot authorize networks for {father_id}: its service {service_id} is not "
            f"on the local registry ({e}), so what that generation was allowed to reach "
            "cannot be re-derived. Every spec this node launches is stored first, so a "
            "missing one is an inconsistent registry, not a normal state."
        ) from e

    for network in networks:
        for spec_net in spec.network:
            if match_networks(network, spec_net):
                filtered.append(network)
                break  # Exit the inner loop

    ancestor_id = sc.get_internal_father_id(id=father_id)
    if sc.internal_instance_exists(id=ancestor_id):
        filtered = filter_networks_with_ancestors(networks=filtered, father_id=ancestor_id)

    return filtered
