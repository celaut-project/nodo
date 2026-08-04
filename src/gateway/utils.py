import os
import shutil
from typing import Generator, Optional

import netifaces as ni

from src.payment_system.ledgers import local_payment_methods
from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.utils import to_gas_amount

env_manager = ConfigManager()

GATEWAY_PORT = env_manager.get("GATEWAY_PORT")
REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")


def generate_node_peer_info(network: str) -> celaut_pb2.Peer:
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
    uri_slot = celaut.Instance.Uri_Slot()
    uri_slot.internal_port = GATEWAY_PORT
    uri_slot.uri.append(uri)
    instance.uri_slot.append(uri_slot)

    slot = celaut.Service.Api.Slot()
    slot.port = GATEWAY_PORT
    slot.transport.CopyFrom(celaut.Service.Api.Protocol(tags=["tcp"]))

    # Advertise what this node charges on a recurring basis, so a peer knows the
    # rate before negotiating anything. The price of a *specific service* is not
    # here: that is what GetServiceEstimatedCost is for. Values are ceilings; see
    # node_advertised_rates(). This rides in the gateway slot because a peer
    # already stores that slot verbatim (manager.add_peer_instance keeps
    # api.slot[0] in peer.protocol_stack), so it needs no schema of its own.
    #
    # Imported here, like local_proofs below: the cost-function package reaches the
    # virtualizer stack, which imports this module back at import time.
    from src.utils.cost_functions.general_cost_functions import node_advertised_rates

    for rate, gas in node_advertised_rates().items():
        slot.gas_amount_per_call[rate].n = str(gas)

    instance.api.slot.append(slot)

    payment_contracts = [e for e in local_payment_methods()]
    log.LOGGER(f'Using {len(payment_contracts)} local payment methods')
    if payment_contracts:
        instance.api.payment_contracts.extend(payment_contracts)

    from src.reputation_system.fetch import local_proofs

    reputation_proofs = list(local_proofs())
    log.LOGGER(f'Using {len(reputation_proofs)} local reputation proofs')

    return celaut_pb2.Peer(
        reputation_proofs=reputation_proofs,
        instance=instance
    )


# If the service is not on the registry, save it.
def save_service(
        metadata: Optional[celaut.Metadata],
        service_dir: str,
        service_hash: str
) -> bool:
    def __save():
        log.LOGGER('Save service on disk')
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

    return os.path.exists(os.path.join(METADATA_REGISTRY, service_hash)) or __save()
