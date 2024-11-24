from time import sleep
from uuid import uuid4
import os

import grpc
from grpcbigbuffer import client as peerpc

import docker as docker_lib

from protos import celaut_pb2 as celaut, gateway_pb2_grpc, gateway_pb2
from protos.gateway_pb2_grpcbf import StartService_input_indices, StartService_input_message_mode
from src.manager.manager import prune_container, spend_gas, update_peer_instance
from src.manager.metrics import gas_amount_on_other_peer
from src.database.sql_connection import SQLConnection, is_peer_available
from src.payment_system.payment_process import increase_deposit_on_peer, init_interfaces
from src.reputation_system.interface import update_reputation, submit_reputation
from src.utils import logger as log
from src.utils.utils import generate_uris_by_peer_id, peers_id_iterator
from src.utils.cost_functions.general_cost_functions import compute_maintenance_cost
from src.utils.env import DOCKER_CLIENT, SHA3_256_ID, EnvManager
from src.utils.tools.duplicate_grabber import DuplicateGrabber
from src.utils.env import EnvManager

env_manager = EnvManager()

MIN_SLOTS_OPEN_PER_PEER = env_manager.get_env("MIN_SLOTS_OPEN_PER_PEER")
MIN_DEPOSIT_PEER = env_manager.get_env("MIN_DEPOSIT_PEER")
TOTAL_REFILLED_DEPOSIT = env_manager.get_env("TOTAL_REFILLED_DEPOSIT")
MANAGER_ITERATION_TIME = env_manager.get_env("MANAGER_ITERATION_TIME")
REGISTRY = env_manager.get_env("REGISTRY")
METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")

sc = SQLConnection()

"""
It doesn't make sense to store this on disk (DB), as each of the elements in the list (str, bool)
requires a search in the pairs to obtain a complete service.
Therefore, the bottleneck is in the number of operations rather than the cost of the object in memory.
Thus, what would make sense, as a control against attacks, is a maximum number of elements in the list,
so that if it 'fills up,' no more elements can enter, and they are not searched until requested again
at some other time when there is space.
"""
wanted_services = {}  # str: bool


def check_wanted_services():
    for wanted in wanted_services.keys():  # TODO async
        if not wanted_services[wanted]:
            wanted_services[wanted] = True
            log.LOGGER(f"Taking the service {wanted}")
            _hash = gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash(
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
                log.LOGGER(f"Using peer {peer}")
                try:
                    for b in peerpc.client_grpc(
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
                        if  type(b) == peerpc.Dir:
                            log.LOGGER(f"    type of dir {b.type}")
                        if type(b) == gateway_pb2.celaut__pb2.Any.Metadata:
                            log.LOGGER("Store the metadata.")
                            with open(f"{METADATA_REGISTRY}{wanted}", "wb") as f:
                                f.write(b.SerializeToString())
                        elif type(b) == peerpc.Dir and b.type == gateway_pb2.celaut__pb2.Service:
                            log.LOGGER(f"Store the service {b.dir}")
                            os.system(f"mv {b.dir} {REGISTRY}{wanted}")
                    del wanted_services[wanted]
                    log.LOGGER(f"Wanted service {wanted} stored successfully.")
                except Exception as e:
                    log.LOGGER(f"Exception on peer {peer} getting a service. {str(e)}. Continue")
                    wanted_services[wanted] = False


def maintain_containers():
    for id in sc.get_all_internal_service_ids():
        try:
            container = DOCKER_CLIENT().containers.get(id)   # TODO refactor with manager.__get_container_by_id()
            if container.status == 'exited':
                update_reputation(token=id, amount=-100)
                log.LOGGER("Prunning container from the registry because the docker container does not exists.")
                prune_container(token=id)
        except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
            log.LOGGER('Exception on maintain container process: ' + str(e))
            continue

        if not spend_gas(
                id=id,
                gas_to_spend=compute_maintenance_cost(
                    system_resources=celaut.Sysresources(
                        mem_limit=sc.get_sys_req(id=id)['mem_limit']
                    )
                )
        ):
            try:
                update_reputation(token=id, amount=-10)  # TODO Needs to update the reputation of the service, not the instance. 
                log.LOGGER("Pruning container due to insufficient gas.")
                prune_container(token=id)
            except Exception as e:
                log.LOGGER('Error purging ' + id + ' ' + str(e))
                raise Exception('Error purging ' + id + ' ' + str(e))
        else:
            update_reputation(token=id, amount=10)


def maintain_clients():
    for client_id in SQLConnection().get_clients_id():
        if SQLConnection().client_expired(client_id=client_id):
            log.LOGGER('Delete client ' + client_id)
            SQLConnection().delete_client(client_id)


def peer_deposits():
    for peer_id in SQLConnection().get_peers_id(): # TODO async
        if not is_peer_available(peer_id=peer_id, min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
            log.LOGGER('Peer '+peer_id+' is not available .')
            try:
                instance = next(peerpc(
                    method=gateway_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(
                            next(generate_uris_by_peer_id(peer_id=peer_id), "")
                        )
                    ).GetInstance,
                    indices_parser=Instance,
                    partitions_message_mode_parser=True
                ), None)
            except:
                continue
            if not instance:
                continue
            try:
                update_peer_instance(
                    instance=instance,
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
        sc.add_client(client_id=f"dev-{uuid4()}", gas=MIN_DEPOSIT_PEER, last_usage=None)
    else:
        client_gas = sc.get_client_gas(client_id=clients[0])[0]
        if client_gas < MIN_DEPOSIT_PEER:
            gas_to_add = MIN_DEPOSIT_PEER - client_gas
            sc.add_gas(client_id=clients[0], gas=gas_to_add)


def manager_thread():
    init_interfaces()
    while True:
        check_wanted_services()
        check_dev_clients()
        maintain_containers()
        submit_reputation()
        maintain_clients()
        peer_deposits()
        DuplicateGrabber().manager()
        sleep(MANAGER_ITERATION_TIME)
