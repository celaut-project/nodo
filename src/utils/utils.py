import base64
import sqlite3
from hashlib import sha256
import socket
from typing import Generator
import typing
from bson.objectid import ObjectId
from grpcbigbuffer.client import Dir
import pymongo
import netifaces as ni

from protos import celaut_pb2 as celaut, gateway_pb2

from src.utils.env import MONGODB, REGISTRY
from src.utils.verify import get_service_hex_main_hash


def read_file(filename) -> bytes:
    def generator(file):
        with open(file, 'rb') as entry:
            for chunk in iter(lambda: entry.read(1024 * 1024), b''):
                yield chunk

    return b''.join([b for b in generator(file=filename)])


def peers_id_iterator(ignore_network: str = None) -> Generator[str, None, None]:
    try:
        for peer in list(pymongo.MongoClient(
                "mongodb://" + MONGODB + "/"
        )["mongo"]["peerInstances"].find()):

            if not ignore_network:
                yield str(peer['_id'])

            elif True not in [address_in_network(
                    ip_or_uri=uri,
                    net=ignore_network
            ) for uri in generate_uris_by_peer_id(
                peer_id=str(peer['_id'])
            )]:
                yield str(peer['_id'])
    except pymongo.errors.ServerSelectionTimeoutError:
        pass


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
    for hash in hashes:
        yield hash


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


get_local_ip_from_network = lambda network: ni.ifaddresses(network)[ni.AF_INET][0]['addr']

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


# TODO MYSQLITE -> Debería de retornar un generador de (ledger, contract address) dada un peer_id y un contract_hash.
"""
    get_ledger_and_contract_address_from_peer_id_and_contract_hash
"""


def get_peer_contract_instances(contract_hash: bytes, peer_id: str) \
        -> Generator[typing.Tuple[str, str], None, None]:
    try:
        # Connect to the SQLite database
        conn = sqlite3.connect('database.db')
        cursor = conn.cursor()

        # Retrieve the peer instance from the 'peer' table
        cursor.execute(
            "SELECT ci.address, l.id "
            "FROM ledger l "
            "JOIN contract_instance ci "
            "ON l.id == ci.ledger_id "
            "WHERE ci.peer_id = ?"
            "AND ci.contract_hash IN ("
            "   SELECT hash FROM contract "
            "   WHERE hash = ?"
            ")", (peer_id, contract_hash,)
        )
        results = cursor.fetchall()

        # Close the database connection
        conn.close()

        if len(results) == 0:
            raise Exception(f"No contract instances for the contract {contract_hash} from peer {peer_id}")

        for result in results:
            yield result[0], result[1]

        # Close the database connection
        conn.close()

    except Exception:
        pass

    raise Exception('No ledger found for contract: ' + str(contract_hash))


def get_peer_id_by_ip(ip: str) -> str:
    try:
        # Connect to the SQLite database
        conn = sqlite3.connect('database.db')
        cursor = conn.cursor()

        # Retrieve the peer ID from the 'peer' table based on the IP
        cursor.execute(
            "SELECT id FROM peer "
            "WHERE id IN ("
            "   SELECT peer_id FROM slot "
            "   WHERE id IN ("
            "       SELECT slot_id FROM uri "
            "       WHERE ip = ?"
            "   )"
            ")", (ip,)
        )
        results = cursor.fetchall()

        # Close the database connection
        conn.close()

        if len(results) == 1:
            return str(results[0][0])
        elif len(results) > 1:
            raise Exception('Multiple peers found for IP: ' + str(ip))
        else:
            raise Exception('No peer found for IP: ' + str(ip))

    except Exception:
        raise Exception('No peer found for IP: ' + str(ip))


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
        # Connect to the SQLite database
        conn = sqlite3.connect('database.db')
        cursor = conn.cursor()

        # Retrieve the peer instance from the 'peer' table
        cursor.execute("SELECT * FROM peer WHERE id = ?", (peer_id,))
        peer = cursor.fetchone()

        if peer:
            # Retrieve the corresponding uris from the 'uri' table
            cursor.execute(
                "SELECT ip, port FROM uri "
                "WHERE slot_id IN ("
                "   SELECT id FROM slot "
                "   WHERE peer_id = ?"
                ")", (peer_id,)
            )
            uris = cursor.fetchall()

            if len(uris) == 0:
                raise Exception('No URIs found for peer: ' + str(peer_id))

            for uri in uris:
                ip, port = uri
                if is_open(ip=ip, port=port):
                    _any = False
                    yield ip + ':' + str(port)

        # Close the database connection
        conn.close()

    except Exception:
        pass


def is_peer_available(peer_id: str, min_slots_open: int = 1) -> bool:
    try:
        return any(generate_uris_by_peer_id(peer_id)) if min_slots_open == 1 else \
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
