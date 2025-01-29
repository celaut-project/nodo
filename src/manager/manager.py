import json
import uuid
from typing import Optional, Generator, Protocol, Tuple

import docker as docker_lib
import grpc
from grpcbigbuffer import client as grpcbf
from google.protobuf.json_format import MessageToJson

from src.manager.resources_manager import IOBigData
from protos import celaut_pb2, gateway_pb2, gateway_pb2_grpc
from src.reputation_system.contracts.ergo.proof_validation import validate_contract_ledger

from src.database.sql_connection import SQLConnection, _split_gas, is_peer_available

from src.utils import logger as log
from src.utils import utils
from src.utils.env import DOCKER_CLIENT, EnvManager
from src.utils.utils import (
    to_gas_amount,
    generate_uris_by_peer_id
)
from src.utils.env import EnvManager
from src.virtualizers.docker.firewall import remove_rule

env_manager = EnvManager()

ALLOW_GAS_DEBT = env_manager.get_env("ALLOW_GAS_DEBT")
DATABASE_FILE = env_manager.get_env("DATABASE_FILE")
MIN_SLOTS_OPEN_PER_PEER = env_manager.get_env("MIN_SLOTS_OPEN_PER_PEER")
DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = env_manager.get_env("DEFAULT_INITIAL_GAS_AMOUNT_FACTOR")
DEFAULT_INTIAL_GAS_AMOUNT = env_manager.get_env("DEFAULT_INTIAL_GAS_AMOUNT")
USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = env_manager.get_env("USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR")
MEMSWAP_FACTOR = env_manager.get_env("MEMSWAP_FACTOR")
FEE_TRIAL_GAS_AMOUNT = int(env_manager.get_env("FREE_TRIAL_GAS_AMOUNT"))

sc = SQLConnection()


def get_dev_clients(gas_amount: int) -> Generator[str, None, None]:
    for client_id in sc.get_dev_clients():
        if sc.get_client_gas(client_id=client_id)[0] > gas_amount:
            yield client_id
            
def add_reputation_proof(contract_ledger, peer_id) -> bool:
    # Verify contract and ledger compatibility and ownership
    if not validate_contract_ledger(contract_ledger, peer_id):
        log.LOGGER(f"Not supported reputation contract ledger {str(contract_ledger)}")
        return False
    
    # Stores on DB
    return sc.add_reputation_proof(contract_ledger=contract_ledger, peer_id=peer_id)

# Insert the instance if it does not exist.
def add_peer_instance(instance: gateway_pb2.Instance) -> Optional[str]:
    if sc.instance_exists(instance):
        return None
    
    parsed_instance = json.loads(MessageToJson(instance))
    log.LOGGER('Inserting instance on db: ' + str(parsed_instance))

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
    for contract_ledger in instance.instance.api.payment_contracts:
        sc.add_contract(contract=contract_ledger, peer_id=peer_id)

    for contract_ledger in instance.instance.api.reputation_proofs:
        add_reputation_proof(contract_ledger=contract_ledger, peer_id=peer_id)

    log.LOGGER(f'Get instance for peer -> {peer_id}')
    return peer_id

def update_peer_instance(instance: gateway_pb2.Instance, peer_id: str):
    log.LOGGER(f"Updating peer {peer_id}")
    # parsed_instance = json.loads(MessageToJson(instance))
    # It is assumed that app protocol and metadata have not been modified.

    # Slots
    for slot in instance.instance.uri_slot:
        sc.add_slot(slot=slot, peer_id=peer_id)

    # Contracts
    for contract_ledger in instance.instance.api.payment_contracts:
        sc.add_contract(contract=contract_ledger, peer_id=peer_id)

    for contract_ledger in instance.instance.api.reputation_proofs:
        add_reputation_proof(contract_ledger=contract_ledger, peer_id=peer_id)

    log.LOGGER(f"Peer {peer_id} updated.")

def get_internal_service_id_by_uri(uri: str) -> str:
    return sc.get_internal_service_id_by_uri(uri=uri)


def __modify_sysreq(id: str, sys_req: celaut_pb2.Sysresources) -> bool:
    if not sc.container_exists(id=id):
        log.LOGGER(f'Manager error: container {id} does not exists.')
        return False
    if sys_req.HasField('mem_limit'):
        variation = sc.get_sys_req(id=id)['mem_limit'] - sys_req.mem_limit
        if variation < 0:
            IOBigData().lock_ram(ram_amount=abs(variation))
        elif variation > 0:
            IOBigData().unlock_ram(ram_amount=variation)
        if variation != 0:
            sc.update_sys_req(id=id, mem_limit=sys_req.mem_limit)
    return True


def __get_container_by_id(id: str) -> docker_lib.models.containers.Container:
    return docker_lib.from_env().containers.get(
        container_id=id
    )


def __refund_gas(
        gas: int = None,
        token: str = None,
        add_function=None,  # Lambda function if cache is not a dict of token:gas
) -> bool:
    try:
        add_function(gas)
    except Exception as e:
        log.LOGGER('Manager error: ' + str(e))
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
    log.LOGGER('Increase local gas for client ' + client_id + ' of ' + str(amount))
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
        id: str,
        gas_to_spend: int,
        refund_gas_function_container: list = None
) -> bool:
    gas_to_spend = int(gas_to_spend)
    try:
        # En caso de que sea un peer, el token es el client id.
        if sc.client_exists(client_id=id):
            log.LOGGER(f"Spend gas for the client {id}.")
            try:
                actual_gas = sc.get_client_gas(client_id=id)
            except Exception as e:
                log.LOGGER(f"actual gas exception {e}")
            
            if not actual_gas: return False
            try:
                actual_gas, last_usage, sci_not = actual_gas
            except Exception as e:
                log.LOGGER(f"actual gas to 3 {e}")
            
            if actual_gas < gas_to_spend and not bool(ALLOW_GAS_DEBT):
                try:
                    gas_to_send_mant, gas_to_send_exp = _split_gas(gas_to_spend)
                except Exception as e:
                    log.LOGGER(f"split gas to spend exceptiocn {e}")
                
                log.LOGGER(f"Insufficient amount of gas {sci_not} from {gas_to_send_mant}e{gas_to_send_exp}")
                return False
            
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
            is_id = sc.container_exists(id=id)
            if not is_id:
                id = sc.get_internal_service_id_by_uri(uri=id)  #  TODO don't should check this at this point.
                is_id = sc.container_exists(id=id) if id else False

            if is_id:
                log.LOGGER(f"Spend gas for the service {id}.")
                current_gas = sc.get_internal_service_gas(id=id)
                if current_gas >= gas_to_spend or ALLOW_GAS_DEBT:
                    sc.update_gas_to_container(id=id, gas=current_gas - gas_to_spend)
                    __refund_gas_function_factory(
                        gas=current_gas,
                        add_function=lambda gas: sc.update_gas_to_container(id=id, gas=gas),  # TODO control race conditions.
                        token=id,
                        container=refund_gas_function_container
                    )
                    return True

    except Exception as e:
        log.LOGGER('Manager error spending gas: ' + str(e))
    return False


def generate_client() -> gateway_pb2.Client:
    # No collisions expected.
    client_id = uuid.uuid4().hex
    sc.add_client(client_id=client_id, gas=FEE_TRIAL_GAS_AMOUNT, last_usage=None)
    log.LOGGER('New client created ' + client_id)
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
        raise Exception('Peer not available.')

    log.LOGGER('Generate new client for peer ' + peer_id)
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
    log.LOGGER('Default cost for ' + (father_id if father_id else 'local'))
    return (int(
        sc.get_gas_amount_by_father_id(id=father_id) * DEFAULT_INITIAL_GAS_AMOUNT_FACTOR)
    ) if father_id and USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR else int(DEFAULT_INTIAL_GAS_AMOUNT)


def add_container(
        father_id: str,
        container: docker_lib.models.containers.Container,
        initial_gas_amount: Optional[int],
        serialized_instance: str,
        system_requirements_range: gateway_pb2.ModifyServiceSystemResourcesInput = None
) -> str:
    
    id: str = container.id
    log.LOGGER(f'Add container for {father_id}')
    initial_gas_amount = initial_gas_amount if initial_gas_amount \
        else default_initial_cost(father_id=father_id)
        
    sc.add_internal_service(
        father_id=father_id,
        container_id=container.id,
        container_ip=container.attrs['NetworkSettings']['IPAddress'],
        gas=initial_gas_amount,
        serialized_instance=serialized_instance
    )
    
    if not container_modify_system_params(
            id=id,
            system_requeriments_range=system_requirements_range
    ):
        log.LOGGER(f'Exception during modify params of {id}.')
        raise Exception(f'Exception during modify params of {id}.')
    
    log.LOGGER(f"Modifed params correctly on token {id}.")
    return id


def container_modify_system_params(
        id: str,
        system_requeriments_range: gateway_pb2.ModifyServiceSystemResourcesInput = None
) -> bool:
    log.LOGGER(f'Modify params of {id}.')

    # https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.Container.update
    # Set system requeriments parameters.

    system_requeriments = system_requeriments_range.max_sysreq  # TODO implement the use of min_sysreq.
    if not system_requeriments: return False

    # TODO Docker has a minimum of 6Mb of mem limit. It should be parametrize on .env and controlled here.
    if __modify_sysreq(
            id=id,
            sys_req=system_requeriments
    ):
        try:
            # Memory limit should be smaller than already set memoryswap limit, update the memoryswap at the same time
            __get_container_by_id(
                id=id
            ).update(
                mem_limit=system_requeriments.mem_limit if MEMSWAP_FACTOR == 0 \
                    else system_requeriments.mem_limit - MEMSWAP_FACTOR * system_requeriments.mem_limit,
                memswap_limit=system_requeriments.mem_limit if MEMSWAP_FACTOR > 0 else -1
            )
        except Exception as e:
            log.LOGGER(f"Docker container for {id} fail with e: {str(e)}")
            # TODO reset modified system req.  Maybe the __get_container_by_id should be inside of __modify_sysreq.
            return False
        return True

    log.LOGGER(f"System req could not be modified for {id}: mem limit {system_requeriments.mem_limit}")
    return False


def could_ve_this_sysreq(sysreq: celaut_pb2.Sysresources) -> bool:
    return IOBigData().prevent_kill(len=sysreq.mem_limit)  # Prevent kill dice de lo que dispone actualmente libre.
    # It's not possible local, but other pair can, returns True.


def get_sysresources(id: str) -> gateway_pb2.ModifyServiceSystemResourcesOutput:
    sys_req = sc.get_sys_req(id=id)
    return gateway_pb2.ModifyServiceSystemResourcesOutput(
        sysreq=celaut_pb2.Sysresources(
            mem_limit=sys_req["mem_limit"],
        ),
        gas=to_gas_amount(
            gas_amount=sc.get_internal_service_gas(id=id)
        )
    )


def prune_container(token: str) -> Optional[int]:  # TODO Should be divided into two functions (for internal and for external), because part of it's use knows if is external or internal before call the function.
    log.LOGGER('Prune container ' + token)
    father_id, serialized_instance = None, None
    
    if sc.container_exists(id=token):
                
        try:
            refund = sc.get_internal_service_gas(id=token)
            sc.purge_internal(id=token)
        except Exception as e:
            log.LOGGER('Error purging ' + token + ' ' + str(e))
            return None
        
        try:
            DOCKER_CLIENT().containers.get(token).remove(force=True)
        except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
            log.LOGGER(str(e) + ' error with docker trying to remove the container with id ' + id)
            return None
        
        father_id = sc.get_internal_father_id(id=token)
        serialized_instance = sc.get_internal_instance(id=token)

    else:
        try:
            external_token = sc.get_token_by_hashed_token(hashed_token=token)
            peer_id = sc.get_peer_id_by_external_service(token=external_token)
            refund = utils.from_gas_amount(
                next(grpcbf.client_grpc(
                    method=gateway_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(
                            next(utils.generate_uris_by_peer_id(peer_id))
                        )
                    ).ModifyGasDeposit,
                        partitions_message_mode_parser=True,
                        indices_parser=gateway_pb2.ModifyGasDepositOutput,
                        input=gateway_pb2.TokenMessage(
                            token=external_token
                        )
                )).amount
            )
            father_id = sc.get_external_father_id(token=external_token)
            serialized_instance = sc.get_external_instance(token=external_token)
        except Exception as e:
            log.LOGGER('Error purging ' + token + ' ' + str(e))
            return None

    # Block the parent's access to the ports of the removed service.
    if sc.container_exists(id=father_id):
        try:
            instance = celaut_pb2.Instance()
            instance.ParseFromString(serialized_instance)
            for slot in instance.instance.uri_slot:
                for uri in slot:
                    if not remove_rule(container_id=father_id, ip=uri.ip, port=uri.port, protocol=Protocol.TCP):
                        log.LOGGER(f"Docker firewall remove rule function failed for the father {father_id}")
                        # TODO This should be controlled.
        except Exception as e:
            log.LOGGER(f"Exception removing rules for the father {father_id}")

    # __refound_gas() # TODO refound gas to parent.
    #  env variable could be used.
    return refund


# Modify Gas Deposit
def modify_gas_deposit(gas_amount: int, service_token: str) -> Tuple[bool, str]:
    
    log.LOGGER(f"Modify {gas_amount} gas of the service {service_token}")
    
    is_internal = sc.container_exists(id=service_token)
    
    father_id: str = sc.get_internal_father_id(id=service_token) if is_internal \
        else sc.get_external_father_id(token=sc.get_token_by_hashed_token(hashed_token=service_token))
        
    if not father_id:
        log.LOGGER(f"ERROR: The service {service_token} (internal {is_internal})  doesn't have father.  This should never happen.")
        return False, 'No father id'
    
    # if gas_amount > father_amount: 
    #   return False, "The father does not have enough gas."    
    #   #  If it cannot, it will throw an exception later.
    
    if gas_amount > 0:
        log.LOGGER(f"Spend gas from father {father_id}")
        if not spend_gas(
                id=father_id,
                gas_to_spend=gas_amount,
                refund_gas_function_container=[]
        ):
            return False, 'Error spending gas'
    
    elif gas_amount < 0:
        # This should be a increase_gas() function, reverse to spend_gas()
        log.LOGGER(f"Add gas to father {father_id}")
        
        if sc.container_exists(id=father_id):
            _gas = sc.get_internal_service_gas(id=father_id)
            _gas += abs(gas_amount)
            sc.update_gas_to_container(id=service_token, gas=_gas)
            
        elif sc.client_exists(client_id=father_id):
            sc.add_gas(client_id=father_id, gas=gas_amount)
        
        else:
            return False, f'ERROR: The father ID {father_id} is neither a client nor an internal service.'
            
        pass
    
    else:
        return True, '0 gas have no sense'
    
    if is_internal:
        current_gas = sc.get_internal_service_gas(id=service_token)
        desired_amount = current_gas+gas_amount
        
        if desired_amount < 0:
            return False, "Negative amount have no sense"
        sc.update_gas_to_container(id=service_token, gas=desired_amount)
    
    else:
        try:
            external_token = sc.get_token_by_hashed_token(hashed_token=service_token)
            peer_id = sc.get_peer_id_by_external_service(token=external_token)
            _output = next(grpcbf.client_grpc(
                method=gateway_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(
                        next(utils.generate_uris_by_peer_id(peer_id))
                    )
                ).ModifyGasDeposit,
                partitions_message_mode_parser=True,
                indices_parser=gateway_pb2.ModifyGasDepositOutput,
                input=gateway_pb2.ModifyGasDepositInput(
                    gas_difference=utils.to_gas_amount(gas_amount),
                    service_token=external_token
                )
            ))
            return _output.success, _output.message
        except Exception as e:
            log.LOGGER(f"Exception on modify_gas_deposit for external service: {e}")
            return False, "Node error."
    
    return True, "Gas modified correctly"
