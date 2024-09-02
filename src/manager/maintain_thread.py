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
from src.payment_system.payment_process import __increase_deposit_on_peer, init_contract_interfaces
from src.reputation_system.simple_reputation_feedback import update_reputation, submit_reputation
from src.utils import logger as l
from src.utils.utils import generate_uris_by_peer_id, peers_id_iterator
from src.utils.cost_functions.general_cost_functions import compute_maintenance_cost
from src.utils.env import DOCKER_CLIENT, MIN_SLOTS_OPEN_PER_PEER, MIN_DEPOSIT_PEER, MANAGER_ITERATION_TIME, REGISTRY, \
    METADATA_REGISTRY, SHA3_256_ID
from src.utils.tools.duplicate_grabber import DuplicateGrabber

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
            l.LOGGER(f"Taking the service {wanted}")
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
                l.LOGGER(f"Using peer {peer}")
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
                        l.LOGGER(f"type of chunk -> {type(b)}")
                        if  type(b) == peerpc.Dir:
                            l.LOGGER(f"    type of dir {b.type}")
                        if type(b) == gateway_pb2.celaut__pb2.Any.Metadata:
                            l.LOGGER("Store the metadata.")
                            with open(f"{METADATA_REGISTRY}{wanted}", "wb") as f:
                                f.write(b.SerializeToString())
                        elif type(b) == peerpc.Dir and b.type == gateway_pb2.celaut__pb2.Service:
                            l.LOGGER(f"Store the service {b.dir}")
                            os.system(f"mv {b.dir} {REGISTRY}{wanted}")
                    del wanted_services[wanted]
                    l.LOGGER(f"Wanted service {wanted} stored successfully.")
                except Exception as e:
                    l.LOGGER(f"Exception on peer {peer} getting a service. {str(e)}. Continue")
                    wanted_services[wanted] = False


def maintain_containers():
    for token in sc.get_all_internal_service_tokens():
        try:
            if DOCKER_CLIENT().containers.get(token.split('##')[-1]).status == 'exited':
                update_reputation(token=token, amount=-100)
                l.LOGGER("Prunning container from the registry because the docker container does not exists.")
                prune_container(token=token)
        except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
            l.LOGGER('Exception on maintain container process: ' + str(e))
            continue

        if not spend_gas(
                id=token,
                gas_to_spend=compute_maintenance_cost(
                    system_resources=celaut.Sysresources(
                        mem_limit=sc.get_sys_req(token=token)['mem_limit']
                    )
                )
        ):
            try:
                update_reputation(token=token, amount=-10)
                l.LOGGER("Pruning container due to insufficient gas.")
                prune_container(token=token)
            except Exception as e:
                l.LOGGER('Error purging ' + token + ' ' + str(e))
                raise Exception('Error purging ' + token + ' ' + str(e))
        else:
            update_reputation(token=token, amount=10)


def maintain_clients():
    for client_id in SQLConnection().get_clients_id():
        if SQLConnection().client_expired(client_id=client_id):
            l.LOGGER('Delete client ' + client_id)
            SQLConnection().delete_client(client_id)


def peer_deposits():
    for peer in SQLConnection().get_peers(): # async
        if not is_peer_available(peer_id=peer['id'], min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
            # l.LOGGER('Peer '+peer_id+' is not available .')
            try:
                update_peer_instance(
                    instance=next(peerpc(
                        method=gateway_pb2_grpc.GatewayStub(
                            grpc.insecure_channel(
                                next(generate_uris_by_peer_id(peer_id=peer['id']))
                            )
                        ).GetInstance,
                        indices_parser=Instance,
                        partitions_message_mode_parser=True
                    )),
                    peer_id=peer["id"]
                )
            except Exception as e:
                l.LOGGER(f"Exception updating peer {peer['id']}: {str(e)}")
                continue

        if peer["gas"] < MIN_DEPOSIT_PEER or \
                gas_amount_on_other_peer(
                    peer_id=peer["id"]
                ) < MIN_DEPOSIT_PEER:
            l.LOGGER(f'\n\n The peer {peer["id"]} has not enough deposit.   ')
            # f'\n   estimated gas deposit -> {peer["gas"]]} '
            # f'\n   min deposit per peer -> {MIN_DEPOSIT_PEER}'
            # f'\n   actual gas deposit -> {gas_amount_on_other_peer(peer_id=peer_id)}'
            # f'\n\n')
            if not __increase_deposit_on_peer(peer_id=peer["id"], amount=MIN_DEPOSIT_PEER):
                l.LOGGER(f'Manager error: the peer {peer["id"]} could not be increased.')


def check_dev_clients():
    sc = SQLConnection()
    clients = sc.get_dev_clients()
    if len(clients) == 0:
        sc.add_client(client_id=f"dev-{uuid4()}", gas=MIN_DEPOSIT_PEER, last_usage=None)
    else:
        client_gas = sc.get_client_gas(client_id=clients[0])[0]
        if client_gas < MIN_DEPOSIT_PEER:
            gas_to_add = MIN_DEPOSIT_PEER - client_gas
            sc.add_gas(client_id=clients[0], gas=gas_to_add)


def manager_thread():
    init_contract_interfaces()
    while True:
        check_wanted_services()
        check_dev_clients()
        maintain_containers()
        # submit_reputation()
        maintain_clients()
        peer_deposits()
        DuplicateGrabber().manager()
        sleep(MANAGER_ITERATION_TIME)
