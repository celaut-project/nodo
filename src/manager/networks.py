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
) -> List[celaut.Instance]:
    # Wildcard "*" (open-internet egress) and any unresolved tag resolve to no
    # concrete peer instances; initialise uris so an unmatched loop cannot raise
    # UnboundLocalError (it previously did for tag "*").
    uris: List[celaut.Instance.Uri] = []
    for tag in network.tags:
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

def match_networks(a: celaut.Service.Network, b: celaut.Service.Network) -> bool:
    # TODO Could be more powerfull
    return bool(set(a.tags) & set(b.tags))  # There is at least one common tag

def filter_networks_with_ancestors(networks: List[celaut.Service.Network], father_id: str) -> List[celaut.Service.Network]:
    """Keep only the networks that every ancestor of ``father_id`` also declares.

    This is the authorization control for ``Service.Network``: a network is usable
    only if it tag-matches the whole ancestor chain. The AND over the chain is
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
