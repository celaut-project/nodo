import socket
from typing import List, Tuple
from protos import celaut_pb2 as celaut


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
            celaut.Instance.Uri(ip=ip, port=80)
            for ip in ips
        ]
    except socket.gaierror:
        raise ValueError(f"Cannot resolve domain: {domain}")

def resolve_network(network: celaut.Service.Network) -> List[celaut.Instance]:
    for tag in network.tags:
        if not tag.islower() or '.' not in tag:
            continue

        uris = resolve_domain(network)
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
