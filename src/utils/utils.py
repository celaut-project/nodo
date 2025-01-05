import os
import socket
import typing
from typing import Generator, Optional

import netifaces as ni
from grpcbigbuffer.block_driver import WITHOUT_BLOCK_POINTERS_FILE_NAME
from grpcbigbuffer.client import Dir

from protos import celaut_pb2 as celaut
from protos import gateway_pb2
from src.database.access_functions.peers import get_peer_ids, get_peer_directions
from src.manager.resources_manager import mem_manager
from src.utils import logger as log
from src.utils.verify import get_service_hex_main_hash
from src.utils.env import EnvManager

env_manager = EnvManager()

REGISTRY = env_manager.get_env("REGISTRY")
METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")

def read_file(filename) -> bytes:
    def generator(file):
        with open(file, 'rb') as entry:
            for chunk in iter(lambda: entry.read(1024 * 1024), b''):
                yield chunk

    return b''.join([b for b in generator(file=filename)])


def get_grpc_uri(instance: celaut.Instance) -> celaut.Instance.Uri:
    for slot in instance.api.slot:
        # if 'grpc' in slot.transport_protocol and 'http2' in slot.transport_protocol: # TODO
        # If the protobuf lib. supported map for this message it could be O(n).
        for uri_slot in instance.uri_slot:
            if uri_slot.internal_port == slot.port:
                return uri_slot.uri[0]
    raise Exception('Grpc over Http/2 not supported on this service ' + str(instance))


def service_hashes(
        hashes: typing.List[gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash]
) -> Generator[celaut.Any.Metadata.HashTag.Hash, None, None]:
    for _hash in hashes:
        yield _hash


def read_service_from_disk(service_hash: str) -> Optional[celaut.Service]:
    log.LOGGER('Getting ' + service_hash + ' service from the local registry.')
    filename: str = os.path.join(REGISTRY, service_hash)
    if not os.path.exists(filename):
        return None

    if os.path.isdir(filename):
        filename = filename + '/' + WITHOUT_BLOCK_POINTERS_FILE_NAME
    try:
        with mem_manager(2 * os.path.getsize(filename)) as iolock:
            service = celaut.Service()
            service.ParseFromString(read_file(filename=filename))
            return service
    except (IOError, FileNotFoundError):
        log.LOGGER('The service was not on registry.')
        return None


def read_metadata_from_disk(service_hash: str) -> Optional[celaut.Any.Metadata]:
    filename: str = os.path.join(METADATA_REGISTRY, service_hash)
    if not os.path.exists(filename):
        return None

    try:
        metadata = celaut.Any.Metadata()
        metadata.ParseFromString(read_file(filename=filename))
        return metadata
    except (IOError, FileNotFoundError):
        log.LOGGER('The metadata was not on registry.')
        return None


def service_extended(
        metadata: celaut.Any.Metadata,
        config: typing.Optional[gateway_pb2.Configuration] = None,
        send_only_hashes: typing.Optional[bool] = False,
        client_id: typing.Optional[str] = None,
        recursion_guard_token: typing.Optional[str] = None
) -> Generator[object, None, None]:
    # 1
    if client_id:
        yield gateway_pb2.Client(
            client_id=client_id
        )

    # 2
    if recursion_guard_token:
        yield gateway_pb2.RecursionGuard(
            token=recursion_guard_token
        )

    # 3
    if config:
        yield config

    # 4
    yield from metadata.hashtag.hash

    if not send_only_hashes:
        # 5
        yield metadata

        # 6
        log.LOGGER(f"Send the service {REGISTRY + get_service_hex_main_hash(metadata=metadata)} using utils.service_extended.")
        yield Dir(
            dir=REGISTRY + get_service_hex_main_hash(metadata=metadata),
            _type=celaut.Service
        )

get_only_the_ip_from_context = lambda context_peer: __get_only_the_ip_from_context_method(context_peer)


def __get_only_the_ip_from_context_method(context_peer: str) -> str:
    try:
        ipv = context_peer.split(':')[0]
        if ipv in ('ipv4', 'ipv6'):
            ip = context_peer[5:-1 * (len(context_peer.split(':')[
                                              -1]) + 1)]  # The format is 'ipv4:49.123.106.100:4442', we don't want 'ipv4:' nor the port.
            return ip[1:-1] if ipv == 'ipv6' else ip
    except Exception as e:
        raise Exception('Error getting the ip from the context: ' + str(e))


get_local_ip_from_network = lambda network: ni.ifaddresses(network)[ni.AF_INET][0]['addr'] \
    if network != "localhost" else network

longestSublistFinder = lambda string1, string2, split: split.join(
    [a for a in string1.split(split) for b in string2.split(split) if a == b]) + split


def __address_in_network(ip_or_uri, net) -> bool:
    #  Return if the ip network portion (addr and broadcast common) is in the ip.
    return (
                   longestSublistFinder(
                       string1=ni.ifaddresses(net)[ni.AF_INET][0]['addr'],
                       string2=ni.ifaddresses(net)[ni.AF_INET][0]['broadcast'],
                       split='.'
                   ) or
                   longestSublistFinder(
                       string1=ni.ifaddresses(net)[ni.AF_INET6][0]['addr'],
                       string2=ni.ifaddresses(net)[ni.AF_INET6][0]['broadcast'],
                       split='::'
                   )
           ) in ip_or_uri \
        if net != 'lo' else \
        ni.ifaddresses(net)[ni.AF_INET][0]['addr'] == ip_or_uri or \
        ni.ifaddresses(net)[ni.AF_INET6][0]['addr'] == ip_or_uri


def get_network_name(direction: str) -> str:
    """
    Get the network name for a given direction. If the direction contains a port, it will be removed.
    If the direction is localhost, it will return 'localhost'.
    
    Args:
        direction (str): The direction to get the network name for.
        
    Returns:
        str: The network name. Returns 'localhost' if no matching network is found.
        
    Raises:
        Exception: If there's an error processing the network interfaces
    """
    direction = direction.replace("http://", "").replace("https://", "").split(':')[0]
    
    # If is localhost
    if "::1" in direction or '0.0.0.0' == direction:
        return "localhost"

    #  https://stackoverflow.com/questions/819355/how-can-i-check-if-an-ip-is-in-a-network-in-python
    try:
        for network in ni.interfaces():
            try:
                if __address_in_network(ip_or_uri=direction, net=network):
                    return network
            except KeyError:
                continue
        
        # If no network is found, return localhost
        return "localhost"
     
    except Exception as e:
        raise Exception('Error getting the network name: ' + str(e))


"""
Gas Amount implementation using float and exponent.  (Currently it's using string)

def to_gas_amount(gas_amount: int) -> gateway_pb2.GasAmount:
    if gas_amount is None: return None
    s: str =  "{:e}".format(gas_amount)
    return gateway_pb2.GasAmount(
        gas_amount = float(s.split('e+')[0]),
        exponent = int(s.split('e+')[1])
    )

def from_gas_amount(gas_amount: gateway_pb2.GasAmount) -> int:
    i: int = str(gas_amount.gas_amount)[::-1].find('.')
    return int(gas_amount.gas_amount * pow(10, i) * pow(10, gas_amount.exponent-i))
"""


def to_gas_amount(gas_amount: int) -> gateway_pb2.GasAmount:
    return gateway_pb2.GasAmount(n=str(gas_amount))


def from_gas_amount(gas_amount: gateway_pb2.GasAmount) -> int:
    return int(gas_amount.n)


def peers_id_iterator(ignore_network: str = None) -> Generator[str, None, None]:
    if ignore_network == "localhost":
        ignore_network = None
    yield from (
        peer_id for peer_id in get_peer_ids()
        if not ignore_network or all(
        not __address_in_network(
            ip_or_uri=uri,
            net=ignore_network
        ) for uri in generate_uris_by_peer_id(
            peer_id=peer_id
        )
    )
    )


def generate_uris_by_peer_id(peer_id: str) -> typing.Generator[str, None, None]:
    yield from (
        ip + ':' + str(port) for ip, port in get_peer_directions(
        peer_id=peer_id
    ) if is_open(ip=ip, port=port)
    )


def is_open(ip: str, port: int) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect((ip, port))
        sock.close()
        return True
    except Exception:
        return False
