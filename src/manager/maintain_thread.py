from time import sleep

from src.manager.manager import MIN_SLOTS_OPEN_PER_PEER, MIN_DEPOSIT_PEER, add_peer, MANAGER_ITERATION_TIME, \
    DOCKER_CLIENT, prune_container, maintain_cost, spend_gas
from src.manager.metrics import gas_amount_on_other_peer
from src.manager.payment_process import __increase_deposit_on_peer, init_contract_interfaces
from src.manager.system_cache import clients, cache_locks, total_deposited_on_other_peers, system_cache
from src.utils.duplicate_grabber import DuplicateGrabber
from src.utils.utils import is_peer_available, peers_id_iterator
import docker as docker_lib
from src.utils import logger as l

def maintain_containers():
    for i in range(len(system_cache)):
        if i >= len(system_cache): break
        token, sysreq = list(system_cache.items())[i]
        try:
            if DOCKER_CLIENT().containers.get(token.split('##')[-1]).status == 'exited':
                prune_container(token=token)
        except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
            l.LOGGER('Exception on maintain container process: ' + str(e))
            continue

        if not spend_gas(
                token_or_container_ip=token,
                gas_to_spend=maintain_cost(sysreq)
        ):
            try:
                prune_container(token=token)
            except Exception as e:
                l.LOGGER('Error purging ' + token + ' ' + str(e))
                raise Exception('Error purging ' + token + ' ' + str(e))


def maintain_clients():
    for client_id, client in clients.items():
        if client.is_expired():
            l.LOGGER('Delete client ' + client_id)
            with cache_locks.lock(client_id):
                del clients[client_id]
            cache_locks.delete(client_id)


def peer_deposits():
    for i in range(len(total_deposited_on_other_peers)):
        if i >= len(total_deposited_on_other_peers): break
        peer_id, estimated_deposit = list(total_deposited_on_other_peers.items())[i]
        if not is_peer_available(peer_id=peer_id, min_slots_open=MIN_SLOTS_OPEN_PER_PEER):
            # l.LOGGER('Peer '+peer_id+' is not available .')
            continue
        if estimated_deposit < MIN_DEPOSIT_PEER or \
                gas_amount_on_other_peer(
                    peer_id=peer_id,
                ) < MIN_DEPOSIT_PEER:
            l.LOGGER('Manager error: the peer ' + str(peer_id) + ' has not enough deposit. ')
            if not __increase_deposit_on_peer(peer_id=peer_id, amount=MIN_DEPOSIT_PEER):
                l.LOGGER('Manager error: the peer ' + str(peer_id) + ' could not be increased.')


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