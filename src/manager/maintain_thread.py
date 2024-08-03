from time import sleep
from uuid import uuid4

import docker as docker_lib

from protos import celaut_pb2 as celaut
from src.manager.manager import prune_container, spend_gas
from src.manager.metrics import gas_amount_on_other_peer
from src.database.sql_connection import SQLConnection, is_peer_available
from src.payment_system.payment_process import __increase_deposit_on_peer, init_contract_interfaces
from src.reputation_system.simple_reputation_feedback import submit_reputation_feedback
from src.utils import logger as l
from src.utils.cost_functions.general_cost_functions import compute_maintenance_cost
from src.utils.env import DOCKER_CLIENT, MIN_SLOTS_OPEN_PER_PEER, MIN_DEPOSIT_PEER, MANAGER_ITERATION_TIME
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
    pass # TODO get services on peers. (balancer or/and gateway.iterable)

def maintain_containers():
    for token in sc.get_all_internal_service_tokens():
        try:
            if DOCKER_CLIENT().containers.get(token.split('##')[-1]).status == 'exited':
                submit_reputation_feedback(token=token, amount=-100)
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
                submit_reputation_feedback(token=token, amount=-10)
                l.LOGGER("Pruning container due to insufficient gas.")
                prune_container(token=token)
            except Exception as e:
                l.LOGGER('Error purging ' + token + ' ' + str(e))
                raise Exception('Error purging ' + token + ' ' + str(e))
        else:
            submit_reputation_feedback(token=token, amount=10)


def maintain_clients():
    for client_id in SQLConnection().get_clients_id():
        if SQLConnection().client_expired(client_id=client_id):
            l.LOGGER('Delete client ' + client_id)
            SQLConnection().delete_client(client_id)

def peer_deposits():

    # Controla el gas que tiene en cada uno de los pares.

    # Vamos a presuponer que tenemos un struct Peer.
    for peer in SQLConnection().get_peers():
        if not is_peer_available(peer_id=peer['id'], min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
            # l.LOGGER('Peer '+peer_id+' is not available .')
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
        maintain_clients()
        peer_deposits()
        DuplicateGrabber().manager()
        sleep(MANAGER_ITERATION_TIME)
