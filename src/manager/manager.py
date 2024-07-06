import json
import sqlite3
import uuid
from hashlib import sha3_256
from typing import Optional

import docker as docker_lib
import grpc
from grpcbigbuffer import client as grpcbf
from google.protobuf.json_format import MessageToJson

from src.manager.resources_manager import IOBigData
from protos import celaut_pb2, gateway_pb2, gateway_pb2_grpc

from src.database.sql_connection import Client, SQLConnection, is_peer_available

from src.utils import logger as logger
from src.utils.env import ALLOW_GAS_DEBT, DATABASE_FILE, MIN_SLOTS_OPEN_PER_PEER, DEFAULT_INITIAL_GAS_AMOUNT_FACTOR, \
    DEFAULT_INTIAL_GAS_AMOUNT, USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR, MEMSWAP_FACTOR, DOCKER_NETWORK, \
    SHA3_256_ID
from src.utils.utils import get_network_name, \
    to_gas_amount, \
    generate_uris_by_peer_id

sc = SQLConnection()


# Insert the instance if it does not exist.
def insert_instance_on_db(instance: gateway_pb2.Instance) -> str:
    parsed_instance = json.loads(MessageToJson(instance))
    logger.LOGGER('Inserting instance on db: ' + str(parsed_instance))

    # Connect to the SQLite database
    with sqlite3.connect(database=DATABASE_FILE) as conn:
        cursor: sqlite3.dbapi2.Cursor = conn.cursor()

        try:

            peer_id = str(uuid.uuid4())
            token: Optional[str] = instance.token if instance.HasField("token") else ""
            metadata: Optional[bytes] = instance.instance_meta.SerializeToString() \
                if instance.HasField('instance_meta') else None
            app_protocol: bytes = instance.instance.api.app_protocol.SerializeToString()

            # Attempt to insert a new row into the 'peer' table
            cursor.execute("INSERT INTO peer (id, token, metadata, app_protocol) VALUES (?, ?, ?, ?)",
                           (peer_id, token, metadata, app_protocol))

            # Slots
            for slot in instance.instance.uri_slot:
                internal_port: int = slot.internal_port
                transport_protocol: bytes = bytes("tcp", "utf-8")
                cursor.execute("INSERT INTO slot (internal_port, transport_protocol, peer_id) VALUES (?, ?, ?)",
                               (internal_port, transport_protocol, peer_id))
                slot_id: int = cursor.lastrowid

                for uri in slot.uri:
                    ip: str = uri.ip
                    port: int = uri.port
                    cursor.execute("INSERT INTO uri (ip, port, slot_id) VALUES (?, ?, ?)",
                                   (ip, port, slot_id))

            print('Contracts on ledger -> ', instance.instance)
            # Contracts
            for contract_ledger in instance.instance.api.contract_ledger:
                contract: bytes = contract_ledger.contract
                address: str = contract_ledger.contract_addr
                ledger: str = contract_ledger.ledger

                contract_hash: str = sha3_256(contract).hexdigest()
                contract_hash_type: str = SHA3_256_ID.hex()

                cursor.execute("INSERT OR IGNORE INTO contract (hash, hash_type, contract) VALUES (?,?,?)",
                               (contract_hash, contract_hash_type, contract))

                cursor.execute("INSERT OR IGNORE INTO ledger (id) VALUES (?)",
                               (ledger,))

                cursor.execute("INSERT INTO contract_instance (address, ledger_id, contract_hash, peer_id) "
                               "VALUES (?,?,?,?)", (address, ledger, contract_hash, peer_id))

            conn.commit()
        except Exception as e:
            # Manage the error
            print("Error on db:", str(e))
            # Revert all changes
            conn.rollback()

        print('Get instance for peer ->', peer_id)

    return peer_id


def get_token_by_uri(uri: str) -> str:
    return sc.get_token_by_uri(uri=uri)

def __modify_sysreq(token: str, sys_req: celaut_pb2.Sysresources) -> bool:
    if token not in sc.system_cache.keys(): raise Exception('Manager error: token ' + token + ' does not exists.')
    if sys_req.HasField('mem_limit'):
        variation = sc.system_cache[token]['mem_limit'] - sys_req.mem_limit

        if variation < 0:
            IOBigData().lock_ram(ram_amount=abs(variation))

        elif variation > 0:
            IOBigData().unlock_ram(ram_amount=variation)

        if variation != 0:
            with sc.cache_locks.lock(token): sc.system_cache[token]['mem_limit'] = sys_req.mem_limit

    return True


def __get_cointainer_by_token(token: str) -> docker_lib.models.containers.Container:
    return docker_lib.from_env().containers.get(
        container_id=token.split('##')[-1]
    )


def __refund_gas(
        gas: int = None,
        token: str = None,
        cache: dict = None,
        add_function=None,  # Lambda function if cache is not a dict of token:gas
) -> bool:
    try:
        with sc.cache_locks.lock(token):
            if add_function:
                add_function(gas)
            elif cache:
                cache[token] += gas
            else:
                raise Exception('Function usage error: Not add_function or cache provided.')
    except Exception as e:
        logger.LOGGER('Manager error: ' + str(e))
        return False
    return True


# Only can be executed once.
def __refund_gas_function_factory(
        gas: int = None,
        cache: dict = None,
        token: str = None,
        container: list = None,
        add_function=None
) -> lambda: None:
    if container:
        container.append(
            lambda: __refund_gas(gas=gas, cache=cache, token=token, add_function=add_function)
        )


def increase_local_gas_for_client(client_id: str, amount: int) -> bool:
    logger.LOGGER('Increase local gas for client ' + client_id + ' of ' + str(amount))
    if client_id not in sc.clients:  # TODO no debería de añadir un peer que no existe.
        raise Exception('Client ' + client_id + ' does not exists.')
    if not __refund_gas(
            gas=amount,
            add_function=lambda gas: sc.clients[client_id].add_gas(gas),
            token=client_id
    ):
        raise Exception('Manager error: cannot increase local gas for client ' + client_id + ' by ' + str(amount))
    return True


def spend_gas(
        token_or_container_ip: str,  # If it's peer, the token is the peer id.
        #  If it's a local service, the token could be the token or the container ip,
        #  on the last case, it takes the token with cache service perspective.
        gas_to_spend: int,
        refund_gas_function_container: list = None
) -> bool:
    gas_to_spend = int(gas_to_spend)
    # logger.LOGGER('Spend '+str(gas_to_spend)+' gas by ' + token_or_container_ip)
    try:
        # En caso de que sea un peer, el token es el peer id.
        if token_or_container_ip in sc.clients and (
                sc.clients[token_or_container_ip].gas >= gas_to_spend or ALLOW_GAS_DEBT):
            with sc.cache_locks.lock(token_or_container_ip):
                sc.clients[token_or_container_ip].reduce_gas(gas=gas_to_spend)
            __refund_gas_function_factory(
                gas=gas_to_spend,
                token=token_or_container_ip,
                add_function=lambda gas: sc.clients[token_or_container_ip].add_gas(gas),
                container=refund_gas_function_container
            )
            return True

        # En caso de que token_or_container_ip sea el token del contenedor.
        elif token_or_container_ip in sc.system_cache and (
                sc.system_cache[token_or_container_ip]['gas'] >= gas_to_spend or ALLOW_GAS_DEBT):
            with sc.cache_locks.lock(token_or_container_ip):
                sc.system_cache[token_or_container_ip]['gas'] -= gas_to_spend
            __refund_gas_function_factory(
                gas=gas_to_spend,
                cache=sc.system_cache,
                token=token_or_container_ip,
                container=refund_gas_function_container
            )
            return True

        # En caso de que token_or_container_ip sea la ip del contenedor.
        token_or_container_ip = sc.cache_service_perspective[token_or_container_ip]
        if token_or_container_ip in sc.system_cache and (
                sc.system_cache[token_or_container_ip]['gas'] >= gas_to_spend or ALLOW_GAS_DEBT):
            with sc.cache_locks.lock(token_or_container_ip):
                sc.system_cache[token_or_container_ip]['gas'] -= gas_to_spend
            __refund_gas_function_factory(
                gas=gas_to_spend,
                cache=sc.system_cache,
                token=token_or_container_ip,
                container=refund_gas_function_container
            )
            return True

    except Exception as e:
        logger.LOGGER('Manager error spending gas: ' + str(e) + ' ' + str(gas_to_spend) + ' ' + token_or_container_ip +
                 '\n peer instances -> ' + str(sc.clients) +
                 '\n system cache -> ' + str(sc.system_cache) +
                 '\n cache service perspective -> ' + str(sc.cache_service_perspective) +
                 '\n        ----------------------\n\n\n')

    return False


def generate_client() -> gateway_pb2.Client:
    # No collisions expected.
    client_id = uuid.uuid4().hex
    sc.clients[client_id] = Client()
    logger.LOGGER('New client created ' + client_id)
    return gateway_pb2.Client(
        client_id=client_id,
    )


def generate_client_id_in_other_peer(peer_id: str) -> Optional[str]:
    if not is_peer_available(peer_id=peer_id, min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
        logger.LOGGER('Peer ' + peer_id + ' is not available.')
        raise Exception('Peer not available.')

    logger.LOGGER('Generate new client for peer ' + peer_id)
    new_client_id = str(next(grpcbf.client_grpc(
            method=gateway_pb2_grpc.GatewayStub(
                grpc.insecure_channel(
                    next(generate_uris_by_peer_id(peer_id=peer_id))
                )
            ).GenerateClient,
            indices_parser=gateway_pb2.Client,
            partitions_message_mode_parser=True
        )).client_id)
    if not SQLConnection().add_external_client(peer_id=peer_id, client_id=new_client_id):
        return  # If fails return None.

    return new_client_id


def add_peer(
        peer_id: str
) -> bool:
    try:
        return SQLConnection().add_peer(peer_id=peer_id)
    except Exception as e:
        print('Error en add_peer', e)
        return False


def default_initial_cost(
        father_id: str = None,
) -> int:
    logger.LOGGER('Default cost for ' + (father_id if father_id else 'local'))
    return (int(
        sc.get_gas_amount_by_id(id=father_id) * DEFAULT_INITIAL_GAS_AMOUNT_FACTOR)
    ) if father_id and USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR else int(DEFAULT_INTIAL_GAS_AMOUNT)


def add_container(
        father_id: str,
        container: docker_lib.models.containers.Container,
        initial_gas_amount: Optional[int],
        system_requirements_range: gateway_pb2.ModifyServiceSystemResourcesInput = None
) -> str:
    logger.LOGGER('Add container for ' + father_id)
    token = father_id + '##' + container.attrs['NetworkSettings']['IPAddress'] + '##' + container.id
    initial_gas_amount = initial_gas_amount if initial_gas_amount else default_initial_cost(
        father_id=father_id)
    sc.add_internal_service(
        father_id=father_id,
        container_id=container.id,
        container_ip=container.attrs['NetworkSettings']['IPAddress'],
        token=token,
        gas=initial_gas_amount
    )
    if not container_modify_system_params(
            token=token,
            system_requeriments_range=system_requirements_range
    ):
        raise Exception('Manager error adding ' + token + '.')
    return token


def container_modify_system_params(
        token: str,
        system_requeriments_range: gateway_pb2.ModifyServiceSystemResourcesInput = None
) -> bool:
    logger.LOGGER('Modify params of ' + token)

    # https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.Container.update
    # Set system requeriments parameters.

    system_requeriments = system_requeriments_range.max_sysreq  # TODO implement the use of min_sysreq.
    if not system_requeriments: return False

    if __modify_sysreq(
            token=token,
            sys_req=system_requeriments
    ):
        try:
            # Memory limit should be smaller than already set memoryswap limit, update the memoryswap at the same time
            __get_cointainer_by_token(
                token=token
            ).update(
                mem_limit=system_requeriments.mem_limit if MEMSWAP_FACTOR == 0 \
                    else system_requeriments.mem_limit - MEMSWAP_FACTOR * system_requeriments.mem_limit,
                memswap_limit=system_requeriments.mem_limit if MEMSWAP_FACTOR > 0 else -1
            )
        except:
            return False
        return True

    return False


def could_ve_this_sysreq(sysreq: celaut_pb2.Sysresources) -> bool:
    return IOBigData().prevent_kill(len=sysreq.mem_limit)  # Prevent kill dice de lo que dispone actualmente libre.
    # It's not possible local, but other pair can, returns True.


def get_sysresources(token: str) -> gateway_pb2.ModifyServiceSystemResourcesOutput:
    return gateway_pb2.ModifyServiceSystemResourcesOutput(
        sysreq=celaut_pb2.Sysresources(
            mem_limit=sc.system_cache[token]["mem_limit"],
        ),
        gas=to_gas_amount(
            gas_amount=sc.system_cache[token]["gas"]
        )
    )


# PRUNE CONTAINER METHOD

def prune_container(token: str) -> int:
    logger.LOGGER('Prune container ' + token)
    if get_network_name(ip_or_uri=token.split('##')[1]) == DOCKER_NETWORK:
        # Suponemos que no tenemos un token externo que empieza por una direccion de nuestra subnet.
        try:
            refund = sc.purgue_internal(
                agent_id=token.split('##')[0],
                container_id=token.split('##')[2],
                container_ip=token.split('##')[1],
                token=token
            )
        except Exception as e:
            logger.LOGGER('Error purging ' + token + ' ' + str(e))
            return False

    else:
        try:
            refund = sc.purgue_external(
                agent_id=token.split('##')[0],
                peer_id=token.split('##')[1],
                his_token=token.split('##')[2],
            )
        except Exception as e:
            logger.LOGGER('Error purging ' + token + ' ' + str(e))
            return False

    # __refound_gas() # TODO refound gas to parent. Need to check what cache is. peer_instances or system_cache.
    #  Podría usar una variable de entorno para hacerlo o no.
    return refund
