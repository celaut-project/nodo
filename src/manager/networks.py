import socket, os, json
from typing import List
from protos import celaut_pb2 as celaut
from src.utils.env import EnvManager

env_manager = EnvManager()

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

        return [
            celaut.Instance.Uri(ip=ip, port=80)  # TODO Must be based on the network client protocol stack
            for ip in ips
        ]
    except socket.gaierror:
        raise ValueError(f"Cannot resolve domain: {domain}")

def resolve_ergo_network() -> List[celaut.Instance.Uri]:
    return []

    # TODO Needs to get the ip and port from the data, actually is the restApiUrl.
    try:
        http_peers_file = env_manager.get_env("ERGO_HTTP_PEERS")
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

def resolve_network(network: celaut.Service.Network) -> List[celaut.Instance]:
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

    client_protocol_stack = network.client_protocol_stack
    i_slot = 1  # Default slot id (because internal port usage is irrelevant here)

    instance = celaut.Instance(
        api=celaut.Service.Api(
            slot=[celaut.Service.Api.Slot(
                port=i_slot,
                protocol_stack=client_protocol_stack
            )],
            payment_contracts=[]
        ),
        uri_slot=celaut.Instance.Uri_Slot(
            internal_port = i_slot,
            uri=uris
        )
    )

    return [instance]
