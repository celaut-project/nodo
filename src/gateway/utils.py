import os
import shutil
from typing import Generator, List, Optional

import netifaces as ni

import src.utils.utils
from src.payment_system.ledgers import generate_contract_ledger
from protos import celaut_pb2 as celaut, gateway_pb2
from src.utils import logger as log
from src.utils.env import EnvManager

env_manager = EnvManager()

GATEWAY_PORT = env_manager.get_env("GATEWAY_PORT")
REGISTRY = env_manager.get_env("REGISTRY")
METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")



def generate_gateway_instance(network: str) -> gateway_pb2.Instance:
    log.LOGGER(f'Generating gateway instance for the network {network}')
    instance = celaut.Instance()

    uri = celaut.Instance.Uri()
    if network == "localhost":
        uri.ip = "127.0.0.1"
        log.LOGGER('Using localhost IP: 127.0.0.1')
    elif network:
        try:
            uri.ip = ni.ifaddresses(network)[ni.AF_INET][0]['addr']
            log.LOGGER(f'Using network interface {network} with IP: {uri.ip}')
        except (ValueError, KeyError, IndexError) as e:
            log.LOGGER('You must specify a valid interface name ' + network)
            raise Exception('Error generating gateway instance --> ' + str(e))
    else:
        raise ValueError('Network interface name cannot be None')

    uri.port = GATEWAY_PORT
    log.LOGGER(f'Setting URI port: {GATEWAY_PORT}')
    uri_slot = celaut.Instance.Uri_Slot()
    uri_slot.internal_port = GATEWAY_PORT
    uri_slot.uri.append(uri)
    instance.uri_slot.append(uri_slot)
    log.LOGGER('URI slot configured')

    slot = celaut.Service.Api.Slot()
    slot.port = GATEWAY_PORT
    instance.api.slot.append(slot)
    log.LOGGER('API slot configured')

    instance.api.payment_contracts.extend(
        [e for e in generate_contract_ledger()]
    )
    log.LOGGER('Payment contracts added to API')

    log.LOGGER('Gateway instance generated')
    return gateway_pb2.Instance(
        instance=instance
    )


# If the service is not on the registry, save it.
def save_service(
        metadata: Optional[celaut.Metadata],
        service_dir: str,
        service_hash: str
) -> bool:
    def __save():
        try:
            shutil.move(service_dir, REGISTRY + service_hash)
            return True
        except Exception as e:
            log.LOGGER(f'Exception saving a service {service_hash}: ' + str(e))
            return False
        finally:
            if metadata:
                try:
                    with open(METADATA_REGISTRY + service_hash, "wb") as f:
                        f.write(metadata.SerializeToString())
                except Exception as e:
                    log.LOGGER(f'Exception writing metadata of {service_hash}: ' + str(e))

    return os.path.isdir(REGISTRY + service_hash) or __save()


def search_container(
        metadata: celaut.Metadata = celaut.Metadata(),
        ignore_network: str = None
) -> Generator[gateway_pb2.buffer__pb2.Buffer, None, None]:
    # Search a service tar container.
    for peer in src.utils.utils.peers_id_iterator(ignore_network=ignore_network):
        try:
            print('SEARCH CONTAINER NOT IMPLEMENTED')
            break
        except Exception as e:
            log.LOGGER('Exception during search container process: ' + str(e))
            pass
