import socket, os, json
from typing import Dict, List, Optional
from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils.config import ConfigManager
from src.utils.utils import read_service_from_disk
from src.manager.network_env import (
    PeerEnvLookup,
    peer_env_matches,
    filter_peers_by_environment,
)

env_manager = ConfigManager()
sc = SQLConnection()

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
    return set(a.tags) & set(b.tags)  # There is at least one common tag

def filter_networks_with_ancestors(networks: List[celaut.Service.Network], father_id: str) -> List[celaut.Service.Network]:
    filtered = []
    service_id = sc.get_service_id_by_container_id(id=father_id)

    spec = read_service_from_disk(service_hash=service_id)
    if not spec:
        return networks
    
    for network in networks:
        for spec_net in spec.network:
            if match_networks(network, spec_net):
                filtered.append(network)
                break  # Exit the inner loop

    ancestor_id = sc.get_internal_father_id(id=father_id)
    if sc.internal_instance_exists(id=ancestor_id):
        filtered = filter_networks_with_ancestors(networks=filtered, father_id=ancestor_id)

    return filtered
