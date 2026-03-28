from time import sleep
from uuid import uuid4
import os

import grpc
from bee_rpc import client as beerpc

from protos import celaut_pb2 as celaut, celaut_pb2_grpc, celaut_pb2
from protos.gateway_bee import StartService_input_indices, StartService_input_message_mode
from src.manager.ergo import check_ergo_node_availability
from src.manager.manager import stop_instance, spend_gas, update_peer_instance
from src.manager.metrics import gas_amount_on_other_peer
from src.database.sql_connection import SQLConnection, is_peer_available
from src.payment_system.payment_process import increase_deposit_on_peer, init_interfaces
from src.reputation_system.interface import update_vmachine_reputation, submit_reputation, update_peer_reputation
from src.utils import logger as log
from src.utils.utils import generate_uris_by_peer_id, peers_id_iterator
from src.utils.cost_functions.general_cost_functions import compute_maintenance_cost
from src.utils.hashing import SHA3_256_ID
from src.utils.config import ConfigManager
from src.utils.tools.duplicate_grabber import DuplicateGrabber
from src.virtualizers.interface import maintain as vm_maintain

env_manager = ConfigManager()

SHORT_INTERVAL_COUNT = env_manager.get("SHORT_INTERVAL_COUNT")
SUBMIT_REPUTATION_AT_INIT = env_manager.get("SUBMIT_REPUTATION_AT_INIT")
MIN_SLOTS_OPEN_PER_PEER = int(env_manager.get("MIN_SLOTS_OPEN_PER_PEER"))
MIN_DEPOSIT_PEER = int(env_manager.get("MIN_DEPOSIT_PEER"))
DEV_CLIENT_GAS_AMOUNT = int(env_manager.get("DEV_CLIENT_GAS_AMOUNT"))
TOTAL_REFILLED_DEPOSIT = int(env_manager.get("TOTAL_REFILLED_DEPOSIT"))
MANAGER_ITERATION_TIME = int(env_manager.get("MANAGER_ITERATION_TIME"))
REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")

DEBUG_MODE = lambda: env_manager.get("DEBUG_MODE")

sc = SQLConnection()

# It doesn't make sense to store this on disk (DB), as each of the elements in the set requires a search in the pairs to obtain a complete service. Therefore, the bottleneck is in the number of operations rather than the cost of the object in memory. Thus, what would make sense, as a control against attacks, is a maximum number of elements in the list, so that if it 'fills up,' no more elements can enter, and they are not searched until requested again at some other time when there is space.

# The mechanism uses two in-memory sets to manage service retrieval requests. The primary set, wanted_services, holds new service IDs to be fetched immediately, while the secondary set, wanted_services_retry, collects IDs that failed retrieval attempts so they can be retried later. This dual-set approach ensures that new requests are processed promptly while providing a controlled way to handle and periodically retry failed requests.

wanted_services = set()
wanted_services_retry = set()

def add_wanted(service_id: str):
    if service_id not in wanted_services and service_id not in wanted_services_retry:
        log.LOGGER(f"Store the service hash on the wanted services set {service_id}")
        wanted_services.add(service_id)

def check_wanted_service(wanted: str):
    log.LOGGER(f"Check wanted service {wanted}")
    # Each execution of the function attempts to retrieve one of the services from the set. If the timeout is high or a large number of pairs are being processed, multiple calls might overlap if the function's execution time exceeds MANAGER_ITERATION_TIME; this is not an issue.
    
    _hash = celaut_pb2.Metadata.HashTag.Hash(
            type=SHA3_256_ID,
            value=bytes.fromhex(wanted)
        )
    for peer in peers_id_iterator():
        """  TODO if get_service cost amount > 0

        if gas_amount_on_other_peer(
                peer_id=peer,
        ) <= cost and not increase_deposit_on_peer(
            peer_id=peer,
            amount=cost
        ):
            raise Exception(
                'Get service error increasing deposit on ' + peer + 'when it didn\'t have enough '
                                                                        'gas.')
        """
        log.LOGGER(f"Taking the service {wanted} using peer {peer}")
        try:
            for b in beerpc.client_grpc(
                    method=celaut_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(
                            next(generate_uris_by_peer_id(peer))
                        )
                    ).GetService,  # TODO An timeout should be implemented when requesting a service.
                    indices_serializer=celaut_pb2.Metadata.HashTag.Hash,
                    input=_hash,
                    indices_parser=StartService_input_indices,  #  Not used all the indices, but still are the same.
                    partitions_message_mode_parser=StartService_input_message_mode
            ):
                if  type(b) == beerpc.Dir:
                    log.LOGGER(f"    type of dir {b.type}")
                    
                if type(b) == celaut_pb2.Metadata:
                    log.LOGGER("Store the metadata.")
                    with open(f"{METADATA_REGISTRY}{wanted}", "wb") as f:
                        f.write(b.SerializeToString())
                elif type(b) == beerpc.Dir and b.type == celaut_pb2.Service:
                    log.LOGGER(f"Store the service {b.dir}")
                    os.system(f"mv {b.dir} {REGISTRY}{wanted}")
                    
            log.LOGGER(f"Wanted service {wanted} stored successfully.")
            return
        
        except Exception as e:
            log.LOGGER(f"Exception on peer {peer} getting the service {wanted}. {str(e)}.")
            continue
    log.LOGGER(f"Any peer was able to get the service {wanted}. (maybe there are not peers available)")
    wanted_services_retry.add(wanted)
            


def maintain_vmachines(debug_mode: bool=False):
    def remove_and_penalize_vmachine(vmachine_id: str):
        update_vmachine_reputation(vmachine_id=vmachine_id, amount=-100)
        log.LOGGER(f"Prunning container {vmachine_id} from the registry because the docker container does not exist.")
        try:
            stop_instance(token=vmachine_id)
        except Exception as e:
            log.LOGGER(f"Error prunning container {vmachine_id}: {e}")
    
    for vmachine_id in sc.get_all_internal_containers_ids():

        # Skip development vmachines from the ggconf command
        if "rundev" in vmachine_id:
            if debug_mode: log.LOGGER(f"Skipping development vmachine {vmachine_id}.")
            continue

        if debug_mode: log.LOGGER(f"Checking vmachine: {vmachine_id}")
        vm_maintain(vmachine_id=vmachine_id, debug_mode=debug_mode, remove_and_penalize=remove_and_penalize_vmachine)
            
        gas_cost = compute_maintenance_cost(
            system_resources=celaut.Sysresources(
                mem_limit=sc.get_sys_req(id=vmachine_id)['mem_limit']
            )
        )
        if debug_mode: log.LOGGER(f"Computed gas cost for {vmachine_id}: {gas_cost:e}")
        
        if not spend_gas(id=vmachine_id, gas_to_spend=gas_cost, debug_mode=debug_mode):
            try:
                update_vmachine_reputation(container_id=vmachine_id, amount=-10)
                log.LOGGER(f"Pruning container {vmachine_id} due to insufficient gas.")
                stop_instance(token=vmachine_id)
            except Exception as e:
                log.LOGGER(f'Error purging {vmachine_id}: {str(e)}')
                raise Exception(f'Error purging {vmachine_id}: {str(e)}')
        else:
            update_vmachine_reputation(container_id=vmachine_id, amount=10)
            if debug_mode: log.LOGGER(f"Updated reputation for {vmachine_id} due to successful maintenance.")

    # Cloud Hypervisor janitor: cleanup stale/orphan runtime resources not tracked by DB.
    try:
        from src.virtualizers.ch.maintain import (
            janitor_cleanup_orphans as ch_janitor_cleanup_orphans,
        )

        ch_janitor_cleanup_orphans(debug_mode=debug_mode)
    except Exception as e:
        log.LOGGER(f"[CH][janitor] failed: {e}")


def maintain_clients(debug_mode: bool=False):
    for client_id in SQLConnection().get_clients_id():
        if debug_mode: log.LOGGER(f"Maintain client {client_id}.")
        if SQLConnection().client_expired(client_id=client_id):
            log.LOGGER('Delete client ' + client_id)
            SQLConnection().delete_client(client_id)


def peer_deposits(debug_mode: bool = False):
    for peer_id in SQLConnection().get_peers_id():
        if debug_mode: log.LOGGER(f"Starting check for peer {peer_id}.")

        if not is_peer_available(peer_id=peer_id, min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
            if debug_mode: log.LOGGER(f"Peer {peer_id} is not available. Attempting to fetch info.")

            try:
                peer = next(beerpc.client_grpc(
                    method=celaut_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(
                            next(generate_uris_by_peer_id(peer_id=peer_id), "")
                        )
                    ).GetPeerInfo,
                    indices_parser=celaut_pb2.Peer,
                    partitions_message_mode_parser=True
                ), None)
                if debug_mode: log.LOGGER(f"Successfully fetched info for peer {peer_id}.")
            except Exception as fetch_exception:
                update_peer_reputation(peer_id=peer_id, amount=-100)
                continue

            if not peer:
                if debug_mode: log.LOGGER(f"No peer info found for {peer_id}. Skipping.")
                continue

            try:
                update_peer_instance(
                    peer=peer,
                    peer_id=peer_id
                )
                if debug_mode: log.LOGGER(f"Peer {peer_id} instance updated successfully.")
            except Exception as update_exception:
                log.LOGGER(f"[ERROR] Exception updating peer {peer_id}: {str(update_exception)}")
                continue
        else:
            if debug_mode: log.LOGGER(f"Peer {peer_id} is available. Skipping info fetch.")

        peer_gas = gas_amount_on_other_peer(peer_id=peer_id)
        if debug_mode: log.LOGGER(f"Peer {peer_id} gas amount: {log.ssformat(peer_gas)}")

        if peer_gas < MIN_DEPOSIT_PEER:
            log.LOGGER(f"[WARNING] The peer {peer_id} has not enough deposit.")
            if debug_mode:
                to_increase = TOTAL_REFILLED_DEPOSIT - peer_gas
                log.LOGGER(
                    f"Insufficient gas details for {peer_id}:\n"
                    f"    - Estimated gas deposit: {log.ssformat(peer_gas)}\n"
                    f"    - Minimum required: {log.ssformat(MIN_DEPOSIT_PEER)}\n"
                    f"    - Amount to refill: {log.ssformat(to_increase)}"
                )

            if not increase_deposit_on_peer(peer_id=peer_id, amount=to_increase):
                log.LOGGER(f"[ERROR] Manager error: the peer {peer_id} could not be increased.")
            else:
                if debug_mode: log.LOGGER(f"Successfully increased deposit for {peer_id}.")
        else:
            if debug_mode: log.LOGGER(f"Peer {peer_id} has sufficient deposit: {log.ssformat(peer_gas)}.")


def check_dev_clients():
    sc = SQLConnection()
    clients = sc.get_dev_clients()
    if len(clients) == 0:
        log.LOGGER("Adds dev client.")
        sc.add_client(client_id=f"dev-{uuid4()}", gas=DEV_CLIENT_GAS_AMOUNT, last_usage=None)
    else:
        client_gas, _, _ = sc.get_client_gas(client_id=clients[0])
        if client_gas < DEV_CLIENT_GAS_AMOUNT:
            gas_to_add = DEV_CLIENT_GAS_AMOUNT - client_gas
            sc.add_gas(client_id=clients[0], gas=gas_to_add)


def manager_thread():

    log.LOGGER("Starting manager thread...")
    print("Starting manager thread...")
    
    # Functions to be executed at the beginning
    init_interfaces()
    check_dev_clients()
    check_ergo_node_availability()
    if SUBMIT_REPUTATION_AT_INIT: submit_reputation(force_submit=True)
    
    short_interval_count = 0
    while True:
        if short_interval_count == int(SHORT_INTERVAL_COUNT):
            short_interval_count = 0
            
            # Functions to be executed every long interval
            check_ergo_node_availability()
            # submit_reputation()    TODO  https://github.com/celaut-project/nodo/issues/80
            check_dev_clients()
            if wanted_services_retry: 
                check_wanted_service(wanted_services_retry.pop())
        
        # Functions to be executed every short interval
        if wanted_services:
            check_wanted_service(wanted_services.pop())  # IMPORTANT! If you want to manually execute this function via a command, you must ensure thread safety.
        maintain_vmachines(debug_mode=DEBUG_MODE())
        maintain_clients(debug_mode=DEBUG_MODE())
        peer_deposits(debug_mode=DEBUG_MODE())
        DuplicateGrabber().manager()
        
        sleep(MANAGER_ITERATION_TIME)
        if DEBUG_MODE():
            log.LOGGER(f"Long interval count: {short_interval_count}/{SHORT_INTERVAL_COUNT}.")
        short_interval_count += 1
