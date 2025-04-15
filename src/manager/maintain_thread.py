from time import sleep
from uuid import uuid4
import os

import grpc
from bee_rpc import client as beerpc

import docker as docker_lib

from protos import celaut_pb2 as celaut, gateway_pb2_grpc, gateway_pb2
from protos.gateway_pb2_bee import StartService_input_indices, StartService_input_message_mode
from src.manager.ergo import check_ergo_node_availability
from src.manager.manager import prune_container, spend_gas, update_peer_instance
from src.manager.metrics import gas_amount_on_other_peer
from src.database.sql_connection import SQLConnection, is_peer_available
from src.payment_system.payment_process import increase_deposit_on_peer, init_interfaces
from src.reputation_system.interface import update_container_reputation, submit_reputation
from src.utils import logger as log
from src.utils.utils import generate_uris_by_peer_id, peers_id_iterator
from src.utils.cost_functions.general_cost_functions import compute_maintenance_cost
from src.utils.env import DOCKER_CLIENT, SHA3_256_ID, EnvManager
from src.utils.tools.duplicate_grabber import DuplicateGrabber
from src.utils.env import EnvManager

env_manager = EnvManager()

SHORT_INTERVAL_COUNT = env_manager.get_env("SHORT_INTERVAL_COUNT")
SUBMIT_REPUTATION_AT_INIT = env_manager.get_env("SUBMIT_REPUTATION_AT_INIT")
MIN_SLOTS_OPEN_PER_PEER = env_manager.get_env("MIN_SLOTS_OPEN_PER_PEER")
MIN_DEPOSIT_PEER = env_manager.get_env("MIN_DEPOSIT_PEER")
DEV_CLIENT_GAS_AMOUNT = env_manager.get_env("DEV_CLIENT_GAS_AMOUNT")
TOTAL_REFILLED_DEPOSIT = env_manager.get_env("TOTAL_REFILLED_DEPOSIT")
MANAGER_ITERATION_TIME = env_manager.get_env("MANAGER_ITERATION_TIME")
REGISTRY = env_manager.get_env("REGISTRY")
METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")

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
    # Each execution of the function attempts to retrieve one of the services from the set. If the timeout is high or a large number of pairs are being processed, multiple calls might overlap if the function's execution time exceeds MANAGER_ITERATION_TIME; this is not an issue.
    
    _hash = gateway_pb2.celaut__pb2.Metadata.HashTag.Hash(
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
                    method=gateway_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(
                            next(generate_uris_by_peer_id(peer))
                        )
                    ).GetService,  # TODO An timeout should be implemented when requesting a service.
                    indices_serializer=StartService_input_indices,
                    indices_parser=StartService_input_indices,
                    partitions_message_mode_parser=StartService_input_message_mode,
                    input=_hash
            ):
                log.LOGGER(f"type of chunk -> {type(b)}")
                if  type(b) == beerpc.Dir:
                    log.LOGGER(f"    type of dir {b.type}")
                    
                if type(b) == gateway_pb2.celaut__pb2.Metadata:
                    log.LOGGER("Store the metadata.")
                    with open(f"{METADATA_REGISTRY}{wanted}", "wb") as f:
                        f.write(b.SerializeToString())
                elif type(b) == beerpc.Dir and b.type == gateway_pb2.celaut__pb2.Service:
                    log.LOGGER(f"Store the service {b.dir}")
                    os.system(f"mv {b.dir} {REGISTRY}{wanted}")
                    
            log.LOGGER(f"Wanted service {wanted} stored successfully.")
            break
        
        except Exception as e:
            log.LOGGER(f"Exception on peer {peer} getting the service {wanted}. {str(e)}.")
            continue
    log.LOGGER(f"Any peer was able to get the service {wanted}. (maybe there are not peers available)")
    wanted_services_retry.add(wanted)
            


def maintain_containers(debug_mode: bool=False):
    def remove_and_penalize_container(container_id):
        update_container_reputation(container_id=container_id, amount=-100)
        log.LOGGER(f"Prunning container {container_id} from the registry because the docker container does not exist.")
        try:
            prune_container(token=container_id)
        except Exception as e:
            log.LOGGER(f"Error prunning container {container_id}: {e}")
    
    for container_id in sc.get_all_internal_containers_ids():
        if debug_mode: log.LOGGER(f"Checking container: {container_id}")
        try:
            container = DOCKER_CLIENT().containers.get(container_id)   # TODO refactor with manager.__get_container_by_id()
            if debug_mode: log.LOGGER(f"Container {container_id} status: {container.status}")
            if container.status == 'exited':
                log.LOGGER(f"Container {container_id} has exited. Removing and penalizing.")
                remove_and_penalize_container(container_id=container_id)
        except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
            log.LOGGER(f"Error fetching container {container_id}: {str(e)}. Assuming it does not exist.")
            remove_and_penalize_container(container_id=container_id)
            
        gas_cost = compute_maintenance_cost(
            system_resources=celaut.Sysresources(
                mem_limit=sc.get_sys_req(id=container_id)['mem_limit']
            )
        )
        if debug_mode: log.LOGGER(f"Computed gas cost for {container_id}: {gas_cost}")
        
        if not spend_gas(id=container_id, gas_to_spend=gas_cost):
            try:
                update_container_reputation(container_id=container_id, amount=-10)
                log.LOGGER(f"Pruning container {container_id} due to insufficient gas.")
                prune_container(token=container_id)
            except Exception as e:
                log.LOGGER(f'Error purging {container_id}: {str(e)}')
                raise Exception(f'Error purging {container_id}: {str(e)}')
        else:
            update_container_reputation(container_id=container_id, amount=10)
            if debug_mode: log.LOGGER(f"Updated reputation for {container_id} due to successful maintenance.")


def maintain_clients():
    for client_id in SQLConnection().get_clients_id():
        if SQLConnection().client_expired(client_id=client_id):
            log.LOGGER('Delete client ' + client_id)
            SQLConnection().delete_client(client_id)


def peer_deposits():
    for peer_id in SQLConnection().get_peers_id(): # TODO async
        if not is_peer_available(peer_id=peer_id, min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
            try:
                peer = next(beerpc(
                    method=gateway_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(
                            next(generate_uris_by_peer_id(peer_id=peer_id), "")
                        )
                    ).GetPeerInfo,
                    indices_parser=gateway_pb2.Peer,
                    partitions_message_mode_parser=True
                ), None)
            except:
                continue
            if not peer:
                continue
            try:
                update_peer_instance(
                    peer=peer,
                    peer_id=peer_id
                )
            except Exception as e:
                log.LOGGER(f"Exception updating peer {peer_id}: {str(e)}")
                continue

        peer_gas = gas_amount_on_other_peer(
                    peer_id=peer_id
                )
        if peer_gas < MIN_DEPOSIT_PEER:
            log.LOGGER(f'\n\n The peer {peer_id} has not enough deposit.   ')
            # f'\n   estimated gas deposit -> {peer["gas"]]} '
            # f'\n   min deposit per peer -> {MIN_DEPOSIT_PEER}'
            # f'\n   actual gas deposit -> {gas_amount_on_other_peer(peer_id=peer_id)}'
            # f'\n\n')
            if not increase_deposit_on_peer(peer_id=peer_id, amount=TOTAL_REFILLED_DEPOSIT-peer_gas):
                log.LOGGER(f'Manager error: the peer {peer_id} could not be increased.')


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
            submit_reputation()
            check_dev_clients()
            check_wanted_service(wanted_services_retry.pop())
        
        # Functions to be executed every short interval
        check_wanted_service(wanted_services.pop())
        maintain_containers(debug_mode=False)
        maintain_clients()
        peer_deposits()
        DuplicateGrabber().manager()
        
        sleep(MANAGER_ITERATION_TIME)
        short_interval_count += 1
