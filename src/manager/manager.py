from uuid import uuid4
from typing import Optional, Generator, Tuple

import grpc
from bee_rpc import client as bee

from src.manager.resources import IOBigData
from protos import celaut_pb2, celaut_pb2, celaut_pb2_grpc
from src.reputation_system.contracts.ergo.proof_validation import validate_contract_ledger as validate_reputation_contract_ledger

from src.database.sql_connection import SQLConnection, is_peer_available

from src.utils import logger as log
from src.utils import utils
from src.utils.config import ConfigManager
from src.utils.utils import (
    from_gas_amount,
    to_gas_amount,
    generate_uris_by_peer_id
)
from src.utils.config import ConfigManager
from src.virtualizers.interface import remove_firewall_rule
from src.virtualizers.interface import kill
from src.virtualizers.interface import hotplug
from src.virtualizers.docker.firewall import TransportProtocol

env_manager = ConfigManager()

ALLOW_GAS_DEBT = env_manager.get("ALLOW_GAS_DEBT")
DATABASE_FILE = env_manager.get("DATABASE_FILE")
MIN_SLOTS_OPEN_PER_PEER = env_manager.get("MIN_SLOTS_OPEN_PER_PEER")
DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = env_manager.get("DEFAULT_INITIAL_GAS_AMOUNT_FACTOR")
DEFAULT_INITIAL_GAS_AMOUNT = env_manager.get("DEFAULT_INITIAL_GAS_AMOUNT")
USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = env_manager.get("USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR")
MEMSWAP_FACTOR = env_manager.get("MEMSWAP_FACTOR")
FEE_TRIAL_GAS_AMOUNT = int(env_manager.get("FREE_TRIAL_GAS_AMOUNT"))
DEV_CLIENT_GAS_AMOUNT = env_manager.get("DEV_CLIENT_GAS_AMOUNT")

sc = SQLConnection()


def get_dev_clients(gas_amount: int) -> Generator[str, None, None]:
    clients = sc.get_dev_clients()
    if len(clients) == 0:
        log.LOGGER("Adds dev client.")
        sc.add_client(client_id=f"dev-{uuid4()}", gas=DEV_CLIENT_GAS_AMOUNT, last_usage=None)
        clients = sc.get_dev_clients()
    for client_id in clients:
        if sc.get_client_gas(client_id=client_id)[0] > gas_amount:
            yield client_id
            
def add_reputation_proof(contract_ledger, peer_id) -> bool:
    # Verify contract and ledger compatibility and ownership
    if not validate_reputation_contract_ledger(contract_ledger, peer_id):
        log.LOGGER(f"Not supported reputation contract.")
        return False
    
    # Stores on DB
    return sc.add_reputation_proof(contract=contract_ledger, peer_id=peer_id)

# Insert the instance if it does not exist.
def add_peer_instance(peer: celaut_pb2.Peer) -> Optional[str]:
    if sc.instance_exists(peer.instance):
        return None

    peer_id = str(uuid4())
    protocol_stack: bytes = peer.instance.api.slot[0].SerializeToString()

    if not sc.add_peer(peer_id=peer_id, protocol_stack=protocol_stack):
        return None

    # Slots
    for slot in peer.instance.uri_slot:
        sc.add_slot(slot=slot, peer_id=peer_id)

    # Contracts
    for gas_price in peer.instance.api.payment_contracts:
        log.LOGGER(f"Adding contract {gas_price.contract} for peer {peer_id}")
        try:
            sc.add_contract(contract=gas_price.contract, peer_id=peer_id, gas_price=from_gas_amount(gas_price.gas_amount))
        except Exception as e:
            log.LOGGER(f"Error adding contract {gas_price.contract} for peer {peer_id}: {e}")

    for contract in peer.reputation_proofs:
        log.LOGGER(f"Adding reputation proof {contract} for peer {peer_id}")
        try:
            if not add_reputation_proof(contract_ledger=contract, peer_id=peer_id):
                log.LOGGER(f"Controlled error to add reputation proof {contract} for peer {peer_id}")
                continue
        except Exception as e:
            log.LOGGER(f"Uncontrolled error adding reputation proof {contract} for peer {peer_id}: {e}")

    print(peer)
    return peer_id

def update_peer_instance(peer: celaut_pb2.Peer, peer_id: str):
    log.LOGGER(f"Updating peer {peer_id}")
    # parsed_peer = json.loads(MessageToJson(peer))
    # It is assumed that protocol stack and metadata have not been modified.

    # Slots
    for slot in peer.instance.uri_slot:
        sc.add_slot(slot=slot, peer_id=peer_id)

    # Contracts
    for gas_price in peer.instance.api.payment_contracts:
        sc.add_contract(contract=gas_price.contract_ledger, peer_id=peer_id, gas_price=from_gas_amount(gas_price.gas_amount))

    for contract_ledger in peer.reputation_proofs:
        if not add_reputation_proof(contract_ledger=contract_ledger, peer_id=peer_id):
            continue

    log.LOGGER(f"Peer {peer_id} updated.")

def get_internal_service_id_by_uri(uri: str) -> str:
    return sc.get_local_instance_id_by_uri(uri=uri)

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
        refund_gas_function_container: list = None,
        debug_mode: bool=True
) -> bool:
    """
    Attempts to deduct gas from a client or container.
    Returns True if successful, False otherwise (with logging on failures).
    """
    gas_to_spend = int(gas_to_spend)
    try:
        is_client = sc.client_exists(client_id=id)
        # If the identifier corresponds to a client
        if is_client:
            client_data = sc.get_client_gas(client_id=id)
            if not client_data:
                log.LOGGER(f"No gas record found for client '{id}'.")
                return False

            actual_gas, last_usage, sci_not = client_data
            actual_gas = int(actual_gas)

            if actual_gas < gas_to_spend and not bool(ALLOW_GAS_DEBT):
                log.LOGGER(f"Insufficient gas for client '{id}': {sci_not} available, needed {log.ssformat(gas_to_spend)}.")
                return False

            if debug_mode: log.LOGGER(f"Reduce {log.ssformat(gas_to_spend)} gas for the client {id}")
            sc.reduce_gas(client_id=id, gas=gas_to_spend)

            __refund_gas_function_factory(
                gas=gas_to_spend,
                token=id,
                add_function=lambda gas: sc.add_gas(client_id=id, gas=gas),
                container=refund_gas_function_container
            )
            return True

        # If the identifier corresponds to a container (by ID or URI)
        else:
            is_id = sc.internal_instance_exists(id=id)
            if not is_id:
                resolved_id = sc.get_local_instance_id_by_uri(uri=id)
                if not resolved_id:
                    return False
                id = resolved_id
                is_id = sc.internal_instance_exists(id=id)
                if not is_id:
                    log.LOGGER(f"Resolved container ID '{id}' does not exist.")
                    return False

            current_gas = sc.get_container_gas(id=id)
            if current_gas < gas_to_spend and not bool(ALLOW_GAS_DEBT):
                log.LOGGER(f"Insufficient gas for container '{id}': {log.ssformat(current_gas)} available, needed {log.ssformat(gas_to_spend)}.")
                return False

            updated_gas = current_gas - gas_to_spend
            if debug_mode: log.LOGGER(f"Container {id} reduced gas from {log.ssformat(current_gas)} to {log.ssformat(updated_gas)} (- {log.ssformat(gas_to_spend)})")
            
            sc.update_gas_to_container(id=id, gas=updated_gas)

            __refund_gas_function_factory(
                gas=gas_to_spend,
                add_function=lambda gas: sc.update_gas_to_container(id=id, gas=gas),
                token=id,
                container=refund_gas_function_container
            )
            return True

    except Exception as e:
        log.LOGGER(f"Manager error spending gas for '{id}': {e}")
        return False


def generate_client() -> celaut_pb2.Client:
    # No collisions expected.
    client_id = uuid4().hex
    sc.add_client(client_id=client_id, gas=FEE_TRIAL_GAS_AMOUNT, last_usage=None)
    log.LOGGER('New client created ' + client_id)
    return celaut_pb2.Client(
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
        5. If the peer is available, generate a new client ID using `bee.client_grpc`.
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
    client_msg = next(bee.client_grpc(
        method=celaut_pb2_grpc.GatewayStub(
            grpc.insecure_channel(
                next(generate_uris_by_peer_id(peer_id=peer_id), "")
            )
        ).GenerateClient,
        indices_parser=celaut_pb2.Client,
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
    ) if father_id and USE_DEFAULT_INITIAL_GAS_AMOUNT_FACTOR else int(DEFAULT_INITIAL_GAS_AMOUNT)

def provision_vmachine(
        service_id: str,
        father_id: str,
        vmachine_id: str,
        vmachine_ip: str,
        initial_gas_amount: Optional[int],
        serialized_instance: str,
        system_requirements_range: celaut_pb2.ModifyServiceSystemResourcesInput = None
):
    
    log.LOGGER(f'Add service for {father_id}')
    initial_gas_amount = initial_gas_amount if initial_gas_amount \
        else default_initial_cost(father_id=father_id)
        
    sc.add_local_instance(
        father_id=father_id,
        container_id=vmachine_id,
        container_ip=vmachine_ip,
        gas=initial_gas_amount,
        serialized_instance=serialized_instance,
        service_id=service_id
    )
    
    if not hotplug(
            vmachine_id=vmachine_id,
            system_requeriments_range=system_requirements_range
    ):
        log.LOGGER(f'Exception during modify params of {vmachine_id}.')
        raise Exception(f'Exception during modify params of {vmachine_id}.')

def get_sysresources(id: str) -> celaut_pb2.ModifyServiceSystemResourcesOutput:
    sys_req = sc.get_sys_req(id=id)
    return celaut_pb2.ModifyServiceSystemResourcesOutput(
        sysreq=celaut_pb2.Sysresources(
            mem_limit=sys_req["mem_limit"],
        ),
        gas=to_gas_amount(
            gas_amount=sc.get_container_gas(id=id)
        )
    )


def stop_instance(token: str) -> Optional[int]:  # TODO Should be divided into two functions (for internal and for external), because part of it's use knows if is external or internal before call the function.
    log.LOGGER('Kill service ' + token)
    father_id, serialized_instance = None, None
    
    if sc.internal_instance_exists(id=token):  # Is internal
        log.LOGGER(f"Token {token} is internal; let's stop it.")
        
        kill(vmachine_id=token)
        
        father_id = sc.get_internal_father_id(id=token)
        serialized_instance = sc.get_internal_instance(id=token)
        
        try:
            refund = sc.get_container_gas(id=token)
            sc.purge_internal(id=token)
            
        except Exception as e:
            log.LOGGER('Error purging ' + token + ' ' + str(e))
            return None

    else:  # It's external
        log.LOGGER(f"Token {token} is external; let's stop it.")
        try:
            external_token = sc.get_delegated_token_by_id(id=token)
            if not external_token:
                log.LOGGER(f"No external token for the token {token}")
                return None
            
            peer_id = sc.get_peer_id_by_external_service(token=external_token)
            if not external_token:
                log.LOGGER(f"No peer for the token {external_token}")
                return None
            
            peer_uri = next(utils.generate_uris_by_peer_id(peer_id))
            if not external_token:
                log.LOGGER(f"No peer uri for the peer {peer_id}")
                return None
            
            refund = utils.from_gas_amount(
                next(bee.client_grpc(
                    method=celaut_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(peer_uri)
                    ).StopService,
                        partitions_message_mode_parser=True,
                        indices_parser=celaut_pb2.ModifyGasDepositOutput,
                        input=celaut_pb2.TokenMessage(token=external_token)
                )).amount
            )
            father_id = sc.get_external_father_id(token=external_token)
            serialized_instance = sc.get_delegated_instance(token=external_token)
            
            sc.purgue_delegated(id=external_token)
            
        except Exception as e:
            log.LOGGER('Error purging external instance with hashed token ' + token + ' ' + str(e))
            return None

    # Block the parent's access to the ports of the removed service.
    if sc.internal_instance_exists(id=father_id):  # Check if the father is an internal instance.
        try:
            instance = celaut_pb2.Instance()
            instance.ParseFromString(serialized_instance)
            for slot in instance.instance.uri_slot:
                for uri in slot:
                    if not remove_firewall_rule(vmachine_id=father_id, ip=uri.ip, port=uri.port, protocol=TransportProtocol.TCP):
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
    
    is_internal = sc.internal_instance_exists(id=service_token)
    
    if is_internal:
        father_id = sc.get_internal_father_id(id=service_token)
    else:
        external_token = sc.get_delegated_token_by_id(id=service_token)
        if not external_token:
            log.LOGGER(f"ERROR: The service {service_token} is not a valid external service.")
            return False, 'Invalid external service token' 

        father_id = sc.get_external_father_id(token=external_token)
        
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
        
        if sc.internal_instance_exists(id=father_id):
            _gas = sc.get_container_gas(id=father_id)
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
        current_gas = sc.get_container_gas(id=service_token)
        desired_amount = current_gas+gas_amount
        
        if desired_amount < 0:
            return False, "Negative amount have no sense"
        sc.update_gas_to_container(id=service_token, gas=desired_amount)
    
    else:
        try:
            external_token = sc.get_delegated_token_by_id(id=service_token)
            if not external_token:
                log.LOGGER(f"No external token for the token {external_token}")
                return False, "No external token found."

            peer_id = sc.get_peer_id_by_external_service(token=external_token)
            if not peer_id:
                log.LOGGER(f"No peer for the token {external_token}")
                return False, "No peer found for the external service."
                
            _output = next(bee.client_grpc(
                method=celaut_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(
                        next(utils.generate_uris_by_peer_id(peer_id))
                    )
                ).ModifyGasDeposit,
                partitions_message_mode_parser=True,
                indices_parser=celaut_pb2.ModifyGasDepositOutput,
                input=celaut_pb2.ModifyGasDepositInput(
                    gas_difference=utils.to_gas_amount(gas_amount),
                    service_token=external_token
                )
            ))
            return _output.success, _output.message
        except Exception as e:
            log.LOGGER(f"Exception on modify_gas_deposit for external service: {e}")
            return False, "Node error."
    
    return True, "Gas modified correctly"
