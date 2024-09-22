import json
import uuid
from typing import Optional, Generator

import docker as docker_lib
import grpc
from grpcbigbuffer import client as grpcbf
from google.protobuf.json_format import MessageToJson

from src.manager.resources_manager import IOBigData
from protos import celaut_pb2, gateway_pb2, gateway_pb2_grpc

from src.database.sql_connection import SQLConnection, is_peer_available

from src.utils import logger as logger
from src.utils.env import EnvManager, DOCKER_NETWORK, \
    SHA3_256_ID
from src.utils.utils import (
    get_network_name,
    to_gas_amount,
    generate_uris_by_peer_id
)
from src.utils.env import EnvManager

env_manager = EnvManager()

ALLOW_GAS_DEBT = env_manager.get_env("ALLOW_GAS_DEBT")
DATABASE_FILE = env_manager.get_env("DATABASE_FILE")
MIN_SLOTS_OPEN_PER_PEER = env_manager.get_env("MIN_SLOTS_OPEN_PER_PEER")
DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = env_manager.get_env("DEFAULT_INITIAL_GAS_AMOUNT_FACTOR")
DEFAULT_INTIAL_GAS_AMOUNT = env_manager.get_env("DEFAULT_INTIAL_GAS_AMOUNT")
USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = env_manager.get_env("USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR")
MEMSWAP_FACTOR = env_manager.get_env("MEMSWAP_FACTOR")

sc = SQLConnection()


def get_dev_clients(gas_amount: int) -> Generator[str, None, None]:
    client_ids = sc.get_dev_clients()
    for client_id in sc.get_dev_clients():
        if sc.get_client_gas(client_id=client_id)[0] > gas_amount:
            yield client_id

# Insert the instance if it does not exist.
def add_peer_instance(instance: gateway_pb2.Instance) -> str:
    parsed_instance = json.loads(MessageToJson(instance))
    logger.LOGGER('Inserting instance on db: ' + str(parsed_instance))

    peer_id = str(uuid.uuid4())
    token: Optional[str] = instance.token if instance.HasField("token") else ""
    metadata: Optional[bytes] = instance.instance_meta.SerializeToString() \
        if instance.HasField('instance_meta') else None
    app_protocol: bytes = instance.instance.api.app_protocol.SerializeToString()

    sc.add_peer(
        peer_id=peer_id, token=token,
        metadata=metadata, app_protocol=app_protocol
        )

    # Slots
    for slot in instance.instance.uri_slot:
        sc.add_slot(slot=slot, peer_id=peer_id)

    # Contracts
    for contract_ledger in instance.instance.api.contract_ledger:
        sc.add_contract(contract=contract_ledger, peer_id=peer_id)

    logger.LOGGER(f'Get instance for peer -> {peer_id}')
    return peer_id

def update_peer_instance(instance: gateway_pb2.Instance, peer_id: str):
    logger.LOGGER(f"Updating peer {peer_id}")
    parsed_instance = json.loads(MessageToJson(instance))
    # It is assumed that app protocol and metadata have not been modified.

    # Slots
    for slot in instance.instance.uri_slot:
        sc.add_slot(slot=slot, peer_id=peer_id)

    # Contracts
    for contract_ledger in instance.instance.api.contract_ledger:
        sc.add_contract(contract=contract_ledger, peer_id=peer_id)

    logger.LOGGER(f"Peer {peer_id} updated.")

def get_token_by_uri(uri: str) -> str:
    return sc.get_token_by_uri(uri=uri)


def __modify_sysreq(token: str, sys_req: celaut_pb2.Sysresources) -> bool:
    if not sc.container_exists(token=token):
        logger.LOGGER(f'Manager error: token {token} does not exists.')
        return False
    if sys_req.HasField('mem_limit'):
        variation = sc.get_sys_req(token=token)['mem_limit'] - sys_req.mem_limit
        if variation < 0:
            IOBigData().lock_ram(ram_amount=abs(variation))
        elif variation > 0:
            IOBigData().unlock_ram(ram_amount=variation)
        if variation != 0:
            sc.update_sys_req(token=token, mem_limit=sys_req.mem_limit)
    return True


def __get_container_by_token(token: str) -> docker_lib.models.containers.Container:
    return docker_lib.from_env().containers.get(
        container_id=token.split('##')[-1]
    )


def __refund_gas(
        gas: int = None,
        token: str = None,
        add_function=None,  # Lambda function if cache is not a dict of token:gas
) -> bool:
    try:
        add_function(gas)
    except Exception as e:
        logger.LOGGER('Manager error: ' + str(e))
        return False
    return True


# Only can be executed once.
def __refund_gas_function_factory(
        gas: int = None,
        token: str = None,
        container: list = None,
        add_function=None
) -> lambda: None:
    if container:
        container.append(
            lambda: __refund_gas(gas=gas, token=token, add_function=add_function)
        )


def increase_local_gas_for_client(client_id: str, amount: int) -> bool:
    logger.LOGGER('Increase local gas for client ' + client_id + ' of ' + str(amount))
    if not sc.client_exists(client_id=client_id):
        raise Exception('Client ' + client_id + ' does not exists.')
    if not __refund_gas(
            gas=amount,
            add_function=lambda gas: sc.add_gas(client_id=client_id, gas=gas),
            token=client_id
    ):
        raise Exception('Manager error: cannot increase local gas for client ' + client_id + ' by ' + str(amount))
    return True


def spend_gas(
        id: str,  # If it's peer, the token is the peer id.
        #  If it's a local service, the token could be the token or the container ip,
        #  on the last case, it takes the token with cache service perspective.
        gas_to_spend: int,
        refund_gas_function_container: list = None
) -> bool:
    gas_to_spend = int(gas_to_spend)
    # logger.LOGGER('Spend '+str(gas_to_spend)+' gas by ' + token_or_container_ip)
    try:
        # En caso de que sea un peer, el token es el client id.
        if sc.client_exists(client_id=id) and (
                sc.get_client_gas(client_id=id)[0] >= gas_to_spend or ALLOW_GAS_DEBT):
            sc.reduce_gas(client_id=id, gas=gas_to_spend)
            __refund_gas_function_factory(
                gas=gas_to_spend,
                token=id,
                add_function=lambda gas: sc.add_gas(client_id=id, gas=gas),
                container=refund_gas_function_container
            )
            return True

        # En caso de que token_or_container_ip sea el token del contenedor.
        else:
            # id could be the container id or container ip. So check first if it's an id. If not, check if it's an ip.
            is_id = sc.container_exists(token=id)
            if not is_id:
                try:
                    id = sc.get_token_by_uri(uri=id)
                    is_id = sc.container_exists(token=id)
                except:
                    is_id = False
            if is_id:
                current_gas = sc.get_internal_service_gas(token=id)
                if current_gas >= gas_to_spend or ALLOW_GAS_DEBT:
                    sc.update_gas_to_container(token=id, gas=current_gas - gas_to_spend)
                    __refund_gas_function_factory(
                        gas=current_gas,
                        add_function=lambda gas: sc.update_gas_to_container(token=id, gas=gas),  # TODO control race conditions.
                        token=id,
                        container=refund_gas_function_container
                    )
                    return True

    except Exception as e:
        logger.LOGGER('Manager error spending gas: ' + str(e) + ' ' + str(gas_to_spend) + '\n        ----------------------\n\n\n')
    return False


def generate_client() -> gateway_pb2.Client:
    # No collisions expected.
    client_id = uuid.uuid4().hex
    sc.add_client(client_id=client_id, gas=0, last_usage=None)
    logger.LOGGER('New client created ' + client_id)
    return gateway_pb2.Client(
        client_id=client_id,
    )


def get_client_id_on_other_peer(peer_id: str) -> Optional[str]:
    """
    Retrieves or generates a client ID for a given peer. If the peer already has an associated client ID for our client,
    it returns that ID. If not, it checks if the peer is available. If the peer is available, it generates a new client ID,
    associates it with the peer, and returns the new client ID.

    Args:
        peer_id (str): The ID of the peer for which to retrieve or generate a client ID for our client.

    Returns:
        Optional[str]: The client ID associated with the peer for our client. Returns None if client ID generation or association fails.

    Raises:
        Exception: If the peer is not available (i.e., it does not have the minimum required open slots).

    Detailed Steps:
        1. Check if the peer already has an associated client ID for our client using `sc.get_peer_client`.
        2. If a client ID is found, return it.
        3. If no client ID is found, check if the peer is available using `is_peer_available`.
        4. If the peer is not available, log the unavailability and raise an exception.
        5. If the peer is available, generate a new client ID using `grpcbf.client_grpc`.
        6. Log the generation of the new client ID.
        7. Attempt to associate the new client ID with the peer using `sc.add_external_client`.
        8. If the association is successful, return the new client ID.
        9. If the association fails, return None.
    """
    client_id = sc.get_peer_client(peer_id=peer_id)
    if client_id: return client_id
    if not is_peer_available(peer_id=peer_id, min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
        logger.LOGGER('Peer ' + peer_id + ' is not available.')
        raise Exception('Peer not available.')

    logger.LOGGER('Generate new client for peer ' + peer_id)
    client_msg = next(grpcbf.client_grpc(
        method=gateway_pb2_grpc.GatewayStub(
            grpc.insecure_channel(
                next(generate_uris_by_peer_id(peer_id=peer_id), "")
            )
        ).GenerateClient,
        indices_parser=gateway_pb2.Client,
        partitions_message_mode_parser=True
    ), "")
    if not client_msg:
        raise Exception("No client msg returned.")
    new_client_id = str(client_msg.client_id)
    if not sc.add_external_client(peer_id=peer_id, client_id=new_client_id):
        return  # If fails return None.

    return new_client_id


def default_initial_cost(
        father_id: str = None,
) -> int:
    logger.LOGGER('Default cost for ' + (father_id if father_id else 'local'))
    return (int(
        sc.get_gas_amount_by_father_id(id=father_id) * DEFAULT_INITIAL_GAS_AMOUNT_FACTOR)
    ) if father_id and USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR else int(DEFAULT_INTIAL_GAS_AMOUNT)


def add_container(
        father_id: str,
        container: docker_lib.models.containers.Container,
        initial_gas_amount: Optional[int],
        system_requirements_range: gateway_pb2.ModifyServiceSystemResourcesInput = None
) -> str:
    logger.LOGGER(f'Add container for {father_id}')
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
        logger.LOGGER(f'Exception during modify params of {token}.')
        raise Exception(f'Exception during modify params of {token}.')
    logger.LOGGER(f"Modifed params correctly on token {token}.")
    return token


def container_modify_system_params(
        token: str,
        system_requeriments_range: gateway_pb2.ModifyServiceSystemResourcesInput = None
) -> bool:
    logger.LOGGER(f'Modify params of {token}.')

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
            __get_container_by_token(
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
    sys_req = sc.get_sys_req(token=token)
    return gateway_pb2.ModifyServiceSystemResourcesOutput(
        sysreq=celaut_pb2.Sysresources(
            mem_limit=sys_req["mem_limit"],
        ),
        gas=to_gas_amount(
            gas_amount=sc.get_internal_service_gas(token=token)["gas"]
        )
    )


# PRUNE CONTAINER METHOD

def prune_container(token: str) -> int:
    logger.LOGGER('Prune container ' + token)
    if get_network_name(ip_or_uri=token.split('##')[1]) == DOCKER_NETWORK:
        # Suponemos que no tenemos un token externo que empieza por una direccion de nuestra subnet.
        try:
            refund = sc.purge_internal(
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
