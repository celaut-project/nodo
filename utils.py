import base64
from hashlib import sha256
import os
import socket
from typing import Generator
import typing

import celaut_pb2, gateway_pb2
from compile import REGISTRY
from grpcbigbuffer import Dir
import pymongo
import netifaces as ni
from verify import get_service_hex_main_hash
from bson.objectid import ObjectId

DEV_CLIENTS = ['192.168.43.200', '192.168.43.39']

def read_file(filename) -> bytes:
    def generator(filename):
        with open(filename, 'rb') as entry:
            for chunk in iter(lambda: entry.read(1024 * 1024), b''):
                yield chunk
    return b''.join([b for b in generator(filename)])


def peers_id_iterator(ignore_network: str = None) -> Generator[str, None, None]:
    for peer in list(pymongo.MongoClient(
                "mongodb://localhost:27017/"
            )["mongo"]["peerInstances"].find()):
        if not ignore_network or ignore_network and True not in [ address_in_network(
            ip_or_uri = uri,
            net = ignore_network
        ) for uri in generate_uris_by_peer_id(
                peer_id = str(peer['_id'])
            ) ]: 
                yield str(peer['_id'])


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
        initial_gas_amount: int = None,
    ) -> Generator[object, None, None]:
        set_config = True if config or initial_gas_amount else False
        for hash in metadata.hashtag.hash:
            if set_config:  # Solo hace falta enviar la configuracion en el primer paquete.
                set_config = False
                yield gateway_pb2.HashWithConfig(
                    hash = hash,
                    config = config,
                    min_sysreq = min_sysreq,
                    max_sysreq = max_sysreq,
                    initial_gas_amount = to_gas_amount(initial_gas_amount)
                )
                continue
            yield hash
        
        if not send_only_hashes:
            print('Sending service ...')
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
                            max_sysreq = max_sysreq,
                            initial_gas_amount = to_gas_amount(initial_gas_amount)
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
    try:
        ipv = context_peer.split(':')[0]
        if ipv in ('ipv4', 'ipv6'):
            ip = context_peer[5:-1*(len(context_peer.split(':')[-1])+1)]  # Lleva el formato 'ipv4:49.123.106.100:4442', no queremos 'ipv4:' ni el puerto.
            return ip[1:-1] if ipv == 'ipv6' else ip
    except Exception as e:
        raise Exception('Error getting the ip from the context: ' + str(e))

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
    try:
        for network in ni.interfaces():
            try:
                if address_in_network(ip_or_uri = ip_or_uri, net = network):
                    return network
            except KeyError:
                continue
    except Exception as e:
        raise Exception('Error getting the network name: ' + str(e))


def get_ledger_and_contract_address_from_peer_id_and_ledger(contract_hash: bytes, peer_id: str) -> typing.Tuple[str, str]:
    try:
        peer = pymongo.MongoClient(
                    "mongodb://localhost:27017/"
                )["mongo"]["peerInstances"].find_one({'_id': ObjectId(peer_id)})

        if sha256(base64.b64decode(peer['instance']['api']['contractLedger'][0]['contract'])).digest() == contract_hash:
            return peer['instance']['api']['contractLedger'][0]['ledger'], peer['instance']['api']['contractLedger'][0]['contractAddr']
    except Exception:
        raise Exception('No ledger found for contract: ' + str(contract_hash))


def get_own_token_from_peer_id(peer_id: str) -> str:
    try:
        return pymongo.MongoClient(
                    "mongodb://localhost:27017/"
                )["mongo"]["peerInstances"].find_one({'_id': ObjectId(peer_id)})['token']
    except Exception:
        raise Exception('No token found for peer: ' + str(peer_id))


def get_peer_id_by_ip(ip: str) -> str:
    if ip in DEV_CLIENTS: return 'dev'
    try:
        return str(pymongo.MongoClient("mongodb://localhost:27017/")["mongo"]["peerInstances"].find_one({'instance.uriSlot.uri.ip': ip})['_id'])
    except Exception:
        raise Exception('No peer found for ip: ' + str(ip))


def is_open(ip: str, port: int) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect((ip, port))
        sock.close()
        return True
    except Exception:
        return False


def generate_uris_by_peer_id(peer_id: str) -> typing.Generator[str, None, None]:
    try:
        peer = pymongo.MongoClient(
                    "mongodb://localhost:27017/"
                )["mongo"]["peerInstances"].find_one({'_id': ObjectId(peer_id)})
        for uri in peer['instance']['uriSlot'][0]['uri']:
            if is_open(ip = uri['ip'], port = int(uri['port'])):
                yield uri['ip'] + ':' + str(uri['port'])
    except Exception:
        raise Exception('No uris found for peer: ' + str(peer_id))


"""
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
    return gateway_pb2.GasAmount(n = str(gas_amount))

def from_gas_amount(gas_amount: gateway_pb2.GasAmount) -> int:
    return int(gas_amount.n)