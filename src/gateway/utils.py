import os
import shutil
import threading
from typing import Generator, Optional

import netifaces as ni

from src.payment_system.ledgers import local_payment_methods, register_local_contracts
from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.utils import to_gas_amount

env_manager = ConfigManager()

GATEWAY_PORT = env_manager.get("GATEWAY_PORT")
REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")


# Guards the lazy recovery below: registering a contract spins up the ledger runtime
# (a JVM and a node connection, for Ergo), so a node that genuinely has no usable
# wallet must not pay that cost on every incoming GetPeerInfo. The lock makes the
# "once per process" bound hold under concurrent GetPeerInfo (gRPC serves it on
# several threads); without it two callers could both retry and spin two JVMs.
_local_contracts_retried = False
_local_contracts_lock = threading.Lock()


def _local_payment_contracts() -> list:
    """The payment contracts to present, recovering from a skipped ledger init.

    ``init()`` writes the LOCAL contract row once at daemon boot and is skipped when
    its runtime dependency is unavailable at that instant — Java installed *after*
    the daemon started, for instance. Presenting ourselves with no payment contract
    makes this node unpayable for as long as it stays up, so retry the registration
    once before announcing nothing.
    """
    global _local_contracts_retried

    contracts = list(local_payment_methods())
    if contracts:
        return contracts

    with _local_contracts_lock:
        # Re-check inside the lock: another thread may have just retried (and
        # possibly registered the contract) while we waited.
        if _local_contracts_retried:
            return list(local_payment_methods())
        _local_contracts_retried = True
        log.LOGGER('No local payment contract registered; retrying the ledger init.')
        register_local_contracts()
        return list(local_payment_methods())


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
    instance.api.slot.append(slot)

    payment_contracts = _local_payment_contracts()
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
