from time import sleep

import docker as docker_lib

from protos import celaut_pb2 as celaut
from src.manager.manager import add_peer, prune_container, spend_gas
from src.manager.metrics import gas_amount_on_other_peer
from src.manager.system_cache import SystemCache
from src.payment_system.payment_process import __increase_deposit_on_peer, init_contract_interfaces
from src.reputation_system.simple_reputation_feedback import submit_reputation_feedback
from src.utils import logger as l
from src.utils.cost_functions.general_cost_functions import compute_maintenance_cost
from src.utils.env import DOCKER_CLIENT, MIN_SLOTS_OPEN_PER_PEER, MIN_DEPOSIT_PEER, MANAGER_ITERATION_TIME
from src.utils.tools.duplicate_grabber import DuplicateGrabber
from src.utils.utils import is_peer_available, peers_id_iterator

sc = SystemCache()


def maintain_containers():
    for i in range(len(sc.system_cache)):
        if i >= len(sc.system_cache):
            break
        token, sysreq = list(sc.system_cache.items())[i]
        try:
            if DOCKER_CLIENT().containers.get(token.split('##')[-1]).status == 'exited':
                submit_reputation_feedback(token=token, amount=-100)
                l.LOGGER("Prunning container from the registry because the docker container does not exists.")
                prune_container(token=token)
        except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
            l.LOGGER('Exception on maintain container process: ' + str(e))
            continue

        if not spend_gas(
                token_or_container_ip=token,
                gas_to_spend=compute_maintenance_cost(
                    system_resources=celaut.Sysresources(
                        mem_limit=sysreq['mem_limit']
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
    for client_id, client in sc.clients.items():
        if client.is_expired():
            l.LOGGER('Delete client ' + client_id)
            with sc.cache_locks.lock(client_id):
                del sc.clients[client_id]
            sc.cache_locks.delete(client_id)


def peer_deposits():
    for i in range(len(sc.total_deposited_on_other_peers)):
        if i >= len(sc.total_deposited_on_other_peers):
            break
        peer_id, estimated_deposit = list(sc.total_deposited_on_other_peers.items())[i]
        if not is_peer_available(peer_id=peer_id, min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
            # l.LOGGER('Peer '+peer_id+' is not available .')
            continue
        if estimated_deposit < MIN_DEPOSIT_PEER or \
                gas_amount_on_other_peer(
                    peer_id=peer_id
                ) < MIN_DEPOSIT_PEER:
            l.LOGGER(f'\n\n The peer {peer_id} has not enough deposit.   ')
            # f'\n   estimated deposit -> {estimated_deposit} '
            # f'\n   min deposit per peer -> {MIN_DEPOSIT_PEER}'
            # f'\n   actual deposit -> {gas_amount_on_other_peer(peer_id=peer_id)}'
            # f'\n\n')
            if not __increase_deposit_on_peer(peer_id=peer_id, amount=MIN_DEPOSIT_PEER):
                l.LOGGER(f'Manager error: the peer {peer_id} could not be increased.')


def load_peer_instances_from_disk():
    for peer in peers_id_iterator():
        add_peer(peer_id=peer)


def manager_thread():
    init_contract_interfaces()
    load_peer_instances_from_disk()
    while True:
        maintain_containers()
        maintain_clients()
        peer_deposits()
        DuplicateGrabber().manager()
        sleep(MANAGER_ITERATION_TIME)
