import socket
from typing import Generator
import typing

from grpcbigbuffer.client import Dir
import netifaces as ni

from src.database.access_functions.peers import get_peer_ids, get_peer_directions
from protos import celaut_pb2 as celaut, gateway_pb2

from src.utils.env import REGISTRY
from src.utils.verify import get_service_hex_main_hash


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


def service_extended(
        metadata: celaut.Any.Metadata,
        config: celaut.Configuration = None,
        min_sysreq: celaut.Sysresources = None,
        max_sysreq: celaut.Sysresources = None,
        send_only_hashes: bool = False,
        initial_gas_amount: int = None,
        client_id: str = None,
        recursion_guard_token: str = None
) -> Generator[object, None, None]:
    set_config = True if config or initial_gas_amount else False

    if client_id:
        yield gateway_pb2.Client(
            client_id=client_id
        )

    if recursion_guard_token:
        yield gateway_pb2.RecursionGuard(
            token=recursion_guard_token
        )

    for _hash in metadata.hashtag.hash:
        if set_config:  # Solo hace falta enviar la configuracion en el primer paquete.
            set_config = False
            yield gateway_pb2.HashWithConfig(
                hash=_hash,
                config=config,
                min_sysreq=min_sysreq,
                max_sysreq=max_sysreq,
                initial_gas_amount=to_gas_amount(initial_gas_amount)
            )
            continue
        yield _hash

    if not send_only_hashes:
        _hash: str = get_service_hex_main_hash(metadata=metadata)
        if set_config:
            print("SERVICE WITH CONFIG NOT SUPPORTED NOW.")
            raise ("SERVICE WITH CONFIG NOT SUPPORTED NOW.")
        else:
            yield (
                gateway_pb2.ServiceWithMeta,
                Dir(REGISTRY + _hash)
            )


def get_free_port() -> int:
    with socket.socket() as s:
        s.bind(('', 0))
        return int(s.getsockname()[1])


get_only_the_ip_from_context = lambda context_peer: get_only_the_ip_from_context_method(context_peer)


def get_only_the_ip_from_context_method(context_peer: str) -> str:
    try:
        ipv = context_peer.split(':')[0]
        if ipv in ('ipv4', 'ipv6'):
            ip = context_peer[5:-1 * (len(context_peer.split(':')[
                                              -1]) + 1)]  # Lleva el formato 'ipv4:49.123.106.100:4442', no queremos 'ipv4:' ni el puerto.
            return ip[1:-1] if ipv == 'ipv6' else ip
    except Exception as e:
        raise Exception('Error getting the ip from the context: ' + str(e))


get_local_ip_from_network = lambda network: ni.ifaddresses(network)[ni.AF_INET][0]['addr'] \
    if network != "localhost" else network

longestSublistFinder = lambda string1, string2, split: split.join(
    [a for a in string1.split(split) for b in string2.split(split) if a == b]) + split


def address_in_network(ip_or_uri, net) -> bool:
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


def get_network_name(ip_or_uri: str) -> str:
    # If is localhost
    if "::1" in ip_or_uri or '0.0.0.0' == ip_or_uri:
        return "localhost"

    #  https://stackoverflow.com/questions/819355/how-can-i-check-if-an-ip-is-in-a-network-in-python
    try:
        for network in ni.interfaces():
            try:
                if address_in_network(ip_or_uri=ip_or_uri, net=network):
                    return network
            except KeyError:
                continue
    except Exception as e:
        raise Exception('Error getting the network name: ' + str(e))


def is_peer_available(peer_id: str, min_slots_open: int = 1) -> bool:
    try:
        return any(list(generate_uris_by_peer_id(peer_id))) if min_slots_open == 1 else \
            len(list(generate_uris_by_peer_id(peer_id))) >= min_slots_open
    except Exception:
        return False


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
    yield from (
        peer_id for peer_id in get_peer_ids()
        if not ignore_network or True not in [
        address_in_network(
            ip_or_uri=uri,
            net=ignore_network
        ) for uri in generate_uris_by_peer_id(
            peer_id=peer_id
        )
    ]
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
