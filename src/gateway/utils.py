import os
import shutil
from typing import Generator, List, Optional

import netifaces as ni

import src.utils.utils
from src.database.access_functions.ledgers import get_ledger_and_contract_addr_from_contract
from src.payment_system.contracts.ethereum.deposit_contract.simulator.interface \
    import CONTRACT_HASH as DEFAULT_PROVISIONAL_CONTRACT_HASH, CONTRACT as DEFAULT_PROVISIONAL_CONTRACT # TODO se mantiene el contrato provisional
from protos import celaut_pb2 as celaut, gateway_pb2
from src.utils import logger as l
from src.utils.env import EnvManager

env_manager = EnvManager()

GATEWAY_PORT = env_manager.get_env("GATEWAY_PORT")
REGISTRY = env_manager.get_env("REGISTRY")
METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")



def __generate_contract_ledger() -> Generator[celaut.Service.Api.ContractLedger, None, None]:
    for address, ledger in get_ledger_and_contract_addr_from_contract(DEFAULT_PROVISIONAL_CONTRACT_HASH):
        contract_ledger = celaut.Service.Api.ContractLedger()
        contract_ledger.contract = DEFAULT_PROVISIONAL_CONTRACT
        contract_ledger.contract_addr, contract_ledger.ledger = address, ledger
        yield contract_ledger


def generate_gateway_instance(network: str) -> gateway_pb2.Instance:
    instance = celaut.Instance()

    uri = celaut.Instance.Uri()
    if network == "localhost":
        uri.ip = "127.0.0.1"
    else:
        try:
            uri.ip = ni.ifaddresses(network)[ni.AF_INET][0]['addr']
        except ValueError as e:
            l.LOGGER('You must specify a valid interface name ' + network)
            raise Exception('Error generating gateway instance --> ' + str(e))

    uri.port = GATEWAY_PORT
    uri_slot = celaut.Instance.Uri_Slot()
    uri_slot.internal_port = GATEWAY_PORT
    uri_slot.uri.append(uri)
    instance.uri_slot.append(uri_slot)

    slot = celaut.Service.Api.Slot()
    slot.port = GATEWAY_PORT
    instance.api.slot.append(slot)

    instance.api.contract_ledger.extend(
        [e for e in __generate_contract_ledger()]
    )
    return gateway_pb2.Instance(
        instance=instance
    )


# If the service is not on the registry, save it.
def save_service(
        metadata: Optional[celaut.Any.Metadata],
        service_dir: str,
        service_hash: str
) -> bool:
    def __save():
        try:
            shutil.move(service_dir, REGISTRY + service_hash)
            return True
        except Exception as e:
            l.LOGGER(f'Exception saving a service {service_hash}: ' + str(e))
            return False
        finally:
            if metadata:
                try:
                    with open(METADATA_REGISTRY + service_hash, "wb") as f:
                        f.write(metadata.SerializeToString())
                except Exception as e:
                    l.LOGGER(f'Exception writing metadata of {service_hash}: ' + str(e))

    return os.path.isdir(REGISTRY + service_hash) or __save()


def search_container(
        metadata: celaut.Any.Metadata = celaut.Any.Metadata(),
        ignore_network: str = None
) -> Generator[gateway_pb2.buffer__pb2.Buffer, None, None]:
    # Search a service tar container.
    for peer in src.utils.utils.peers_id_iterator(ignore_network=ignore_network):
        try:
            print('SEARCH CONTAINER NOT IMPLEMENTED')
            break
        except Exception as e:
            l.LOGGER('Exception during search container process: ' + str(e))
            pass


def search_file(hashes: List[gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash], ignore_network: str = None) -> \
Generator[celaut.Any, None, None]:
    print('SEARCH FILE METHOD NOT IMPLEMENTED.')
    raise Exception('SEARCH FILE METHOD NOT IMPLEMENTED.')



def search_definition(hashes: List[gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash], ignore_network: str = None) \
        -> celaut.Service:
    l.LOGGER('SEARCH DEFINITION NOT IMPLEMENTED.')
    raise Exception('SEARCH DEFINITION NOT IMPLEMENTED.')
