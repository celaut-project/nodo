import socket, os
from typing import Generator

import celaut_pb2, gateway_pb2
from compile import REGISTRY
from grpcbigbuffer import Dir
import pymongo
import netifaces as ni
from verify import get_service_hex_main_hash

GET_ENV = lambda env, default: type(default)(os.environ.get(env)) if env in os.environ.keys() else default

def read_file(filename) -> bytes:
    def generator(filename):
        with open(filename, 'rb') as entry:
            for chunk in iter(lambda: entry.read(1024 * 1024), b''):
                yield chunk
    return b''.join([b for b in generator(filename)])

def peers_iterator(ignore_network: str = None) -> Generator[celaut_pb2.Instance.Uri, None, None]:
    peers = list(pymongo.MongoClient(
                "mongodb://localhost:27017/"
            )["mongo"]["peerInstances"].find())

    for peer in peers:
        peer_uri = peer['uriSlot'][0]['uri'][0]
        if not ignore_network or ignore_network and not address_in_network(
            ip_or_uri = peer_uri['ip'],
            net = ignore_network
        ): yield peer_uri

def get_grpc_uri(instance: celaut_pb2.Instance) -> celaut_pb2.Instance.Uri:
    for slot in instance.api.slot:
        #if 'grpc' in slot.transport_protocol and 'http2' in slot.transport_protocol: # TODO
        # If the protobuf lib. supported map for this message it could be O(n).
        for uri_slot in instance.uri_slot:
            if uri_slot.internal_port == slot.port:
                return uri_slot.uri[0]
    raise Exception('Grpc over Http/2 not supported on this service ' + str(instance))

def service_hashes(
        hashes: list = []
    ) -> Generator[celaut_pb2.Any.Metadata.HashTag.Hash, None, None]:
        for hash in hashes:
            yield hash 

def service_extended(
        service_buffer: bytes,
        metadata: celaut_pb2.Any.Metadata,  
        config: celaut_pb2.Configuration = None,
        min_sysreq: celaut_pb2.Sysresources = None,
        max_sysreq: celaut_pb2.Sysresources = None,
        send_only_hashes: bool = False,
    ) -> Generator[object, None, None]:
        set_config = True if config else False
        for hash in metadata.hashtag.hash:
            if set_config:  # Solo hace falta enviar la configuracion en el primer paquete.
                set_config = False
                yield gateway_pb2.HashWithConfig(
                    hash = hash,
                    config = config,
                    min_sysreq = min_sysreq,
                    max_sysreq = max_sysreq,
                )
                continue
            yield hash
        
        if not send_only_hashes:
            any = celaut_pb2.Any(
                    metadata = metadata,
                    value = service_buffer
                )
            hash = get_service_hex_main_hash(service_buffer = service_buffer, metadata = metadata)
            if set_config: 
                yield (
                        gateway_pb2.ServiceWithConfig,
                        gateway_pb2.ServiceWithConfig(
                            service = any,
                            config = config,
                            min_sysreq = min_sysreq,
                            max_sysreq = max_sysreq
                        ),
                        Dir(REGISTRY + hash + '/p2')
                    )
            else:
                yield (
                        gateway_pb2.ServiceWithMeta,
                        any,
                        Dir(REGISTRY + hash + '/p2')
                    )

def get_free_port() -> int:
    with socket.socket() as s:
        s.bind(('', 0))
        return int(s.getsockname()[1])

get_only_the_ip_from_context = lambda context_peer: get_only_the_ip_from_context_method(context_peer)

def get_only_the_ip_from_context_method(context_peer: str) -> str:
    ipv = context_peer.split(':')[0]
    if ipv in ('ipv4', 'ipv6'):
        ip = context_peer[5:-1*(len(context_peer.split(':')[-1])+1)]  # Lleva el formato 'ipv4:49.123.106.100:4442', no queremos 'ipv4:' ni el puerto.
        return ip[1:-1] if ipv == 'ipv6' else ip

get_local_ip_from_network = lambda network: ni.ifaddresses(network)[ni.AF_INET][0]['addr']

longestSublistFinder = lambda string1, string2, split: split.join([a for a in string1.split(split) for b in string2.split(split) if a == b])+split

def address_in_network( ip_or_uri, net) -> bool:
    #  Return if the ip network portion (addr and broadcast common) is in the ip.
    return (
            longestSublistFinder(
                string1 = ni.ifaddresses(net)[ni.AF_INET][0]['addr'],
                string2 = ni.ifaddresses(net)[ni.AF_INET][0]['broadcast'],
                split = '.'
            ) or \
            longestSublistFinder(
                string1 = ni.ifaddresses(net)[ni.AF_INET6][0]['addr'],
                string2 = ni.ifaddresses(net)[ni.AF_INET6][0]['broadcast'],
                split = '::'
            ) 
        ) in ip_or_uri \
        if net != 'lo' else \
            ni.ifaddresses(net)[ni.AF_INET][0]['addr'] == ip_or_uri or \
            ni.ifaddresses(net)[ni.AF_INET6][0]['addr'] == ip_or_uri


def get_network_name( ip_or_uri: str) -> str:
    #  https://stackoverflow.com/questions/819355/how-can-i-check-if-an-ip-is-in-a-network-in-python
    for network in ni.interfaces():
        try:
            if address_in_network(ip_or_uri = ip_or_uri, net = network):
                return network
        except KeyError:
            continue