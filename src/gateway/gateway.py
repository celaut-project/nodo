import itertools
import os
import shutil
import threading
from concurrent import futures
from typing import Generator

import docker as docker_lib
import grpc
import grpcbigbuffer as grpcbf
import netifaces as ni

import iobigdata as iobd
from contracts.eth_main.utils import get_ledger_and_contract_addr_from_contract
from protos import celaut_pb2 as celaut, gateway_pb2, gateway_pb2_grpc
from protos import gateway_pb2_grpcbf
from protos.gateway_pb2_grpcbf import GetServiceTar_input
from src import utils
from src.compiler.compile import REGISTRY, HYCACHE
from src.gateway.server import Gateway
from src.manager.manager import COMPUTE_POWER_RATE, COST_OF_BUILD, EXECUTION_BENEFIT, MANAGER_ITERATION_TIME, \
    COST_AVERAGE_VARIATION, GAS_COST_FACTOR, MODIFY_SERVICE_SYSTEM_RESOURCES_COST
from src.manager.maintain_thread import manager_thread
from src.manager.manager import DOCKER_NETWORK, LOCAL_NETWORK
from src.utils import logger as l
from src.utils.logger import GET_ENV
from src.utils.verify import check_service, get_service_hex_main_hash
from src.utils import utils as utils

DOCKER_CLIENT = lambda: docker_lib.from_env(
    timeout=GET_ENV(env='DOCKER_CLIENT_TIMEOUT', default=480),
    max_pool_size=GET_ENV(env='DOCKER_MAX_CONNECTIONS', default=1000)
)
GATEWAY_PORT = GET_ENV(env='GATEWAY_PORT', default=8090)
MEMORY_LOGS = GET_ENV(env='MEMORY_LOGS', default=False)
IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER = GET_ENV(env='IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER', default=True)
SEND_ONLY_HASHES_ASKING_COST = GET_ENV(env='SEND_ONLY_HASHES_ASKING_COST', default=False)
DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH = GET_ENV(env='DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH', default=False)
GENERAL_WAIT_TIME = GET_ENV(env='GENERAL_WAIT_TIME', default=2)
GENERAL_ATTEMPTS = GET_ENV(env='GENERAL_ATTEMPTS', default=10)
CONCURRENT_CONTAINER_CREATIONS = GET_ENV(env='CONCURRENT_CONTAINER_CREATIONS', default=10)

#  TODO auxiliares
DEFAULT_PROVISIONAL_CONTRACT = open('../../contracts/vyper_gas_deposit_contract/bytecode', 'rb').read()
from contracts.vyper_gas_deposit_contract.interface import CONTRACT_HASH as DEFAULT_PROVISIONAL_CONTRACT_HASH


def generate_contract_ledger() -> celaut.Service.Api.ContractLedger:  # TODO generate_contract_ledger tambien es un método auxiliar.
    contract_ledger = celaut.Service.Api.ContractLedger()
    contract_ledger.contract = DEFAULT_PROVISIONAL_CONTRACT
    contract_ledger.ledger, contract_ledger.contract_addr = \
        get_ledger_and_contract_addr_from_contract(DEFAULT_PROVISIONAL_CONTRACT_HASH)[0].values()
    return contract_ledger


def generate_gateway_instance(network: str) -> gateway_pb2.Instance:
    instance = celaut.Instance()

    uri = celaut.Instance.Uri()
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

    instance.api.contract_ledger.append(generate_contract_ledger())
    return gateway_pb2.Instance(
        instance=instance
    )


# If the service is not on the registry, save it.
def save_service(
        service_p1: bytes,
        service_p2: str,
        metadata: celaut.Any.Metadata,
        service_hash: str = None
) -> str:
    if not service_p1 or not service_p2:
        l.LOGGER('Save service partitions required.')
        raise Exception('Save service partitions required.')

    if not service_hash:
        service_hash = get_service_hex_main_hash(
            service_buffer=(service_p1, service_p2),
            metadata=metadata
        )
    if not os.path.isdir(REGISTRY + service_hash):
        os.mkdir(REGISTRY + service_hash)
        with open(
                REGISTRY + service_hash + '/p1', 'wb'
        ) as file:  # , iobd.mem_manager(len=len(service_p1)): TODO check mem-58 bug.
            file.write(
                celaut.Any(
                    metadata=metadata,
                    value=service_p1
                ).SerializeToString()
            )
        if service_p2:
            shutil.move(service_p2, REGISTRY + service_hash + '/p2')
    return service_hash


def search_container(
        service_buffer: bytes,
        metadata: celaut.Any.Metadata = celaut.Any.Metadata(),
        ignore_network: str = None
) -> Generator[gateway_pb2.buffer__pb2.Buffer, None, None]:
    # Search a service tar container.
    for peer in utils.peers_id_iterator(ignore_network=ignore_network):
        try:
            yield next(grpcbf.client_grpc(
                method=gateway_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(
                        next(utils.generate_uris_by_peer_id(peer)),
                    )
                ).GetServiceTar,
                input=utils.service_extended(
                    service_buffer=service_buffer,
                    metadata=metadata
                ),
                indices_serializer=GetServiceTar_input
            ))
            break
        except Exception as e:
            l.LOGGER('Exception during search container process: ' + str(e))
            pass


def search_file(hashes: list, ignore_network: str = None) -> Generator[celaut.Any, None, None]:
    # TODO: It can search for other 'Service ledger' or 'ANY ledger' instances that could've this type of files.
    for peer in utils.peers_id_iterator(ignore_network=ignore_network):
        try:
            for buffer in grpcbf.client_grpc(
                    method=gateway_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(
                            next(utils.generate_uris_by_peer_id(peer)),
                        )
                    ).GetFile,
                    output_field=celaut.Any,
                    input=utils.service_hashes(
                        hashes=hashes
                    )
            ):
                yield buffer
        except Exception as e:
            l.LOGGER('Exception during search file process: ' + str(e))
            pass


def search_definition(hashes: list, ignore_network: str = None) -> bytes:
    #  Search a service description.
    for any in search_file(
            hashes=hashes,
            ignore_network=ignore_network
    ):
        if check_service(
                service_buffer=any.value,
                hashes=hashes
        ):
            service_partitions_iterator = grpcbf.parse_from_buffer.conversor(
                iterator=itertools.chain([any.value]),
                pf_object=gateway_pb2.ServiceWithMeta,
                remote_partitions_model=gateway_pb2_grpcbf.StartService_input_partitions_v2[2],
                mem_manager=iobd.mem_manager,
                yield_remote_partition_dir=False,
                partitions_message_mode=[True, False]
            )
            #  Save the service on the registry.
            save_service(
                service_p1=next(service_partitions_iterator),
                service_p2=next(service_partitions_iterator),
                metadata=any.metadata
            )
            return any.value

    l.LOGGER('The service ' + hashes[0].value.hex() + ' was not found.')
    raise Exception('The service ' + hashes[0].value.hex() + ' was not found.')


def get_service_buffer_from_registry(service_hash: str) -> bytes:
    return get_from_registry(service_hash=service_hash).value


def get_from_registry(service_hash: str) -> celaut.Any:
    l.LOGGER('Getting ' + service_hash + ' service from the local registry.')
    first_partition_dir = REGISTRY + service_hash + '/p1'
    try:
        with iobd.mem_manager(2 * os.path.getsize(first_partition_dir)) as iolock:
            any = celaut.Any()
            any.ParseFromString(utils.read_file(filename=first_partition_dir))
            return any
    except (IOError, FileNotFoundError):
        l.LOGGER('The service was not on registry.')
        raise FileNotFoundError


def main():
    # Create __hycache__ if it does not exists.
    try:
        os.system('mkdir ' + HYCACHE)
    except:
        pass

    # Create __registry__ if it does not exists.
    try:
        os.system('mkdir ' + REGISTRY)
    except:
        pass

    from src.utils.zeroconf import Zeroconf
    import iobigdata
    from psutil import virtual_memory

    iobigdata.IOBigData(
        ram_pool_method=lambda: virtual_memory().total
    ).set_log(
        log=l.LOGGER if MEMORY_LOGS else lambda message: None
    )

    grpcbf.modify_env(
        cache_dir=HYCACHE,
        mem_manager=iobigdata.mem_manager
    )

    # Zeroconf for connect to the network (one per network).
    for network in ni.interfaces():
        if network != DOCKER_NETWORK and network != LOCAL_NETWORK:
            Zeroconf(network=network)

    # Run manager.
    threading.Thread(
        target=manager_thread,
        daemon=True
    ).start()

    # create a gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=30))
    gateway_pb2_grpc.add_GatewayServicer_to_server(
        Gateway(), server=server
    )

    SERVICE_NAMES = (
        gateway_pb2.DESCRIPTOR.services_by_name['Gateway'].full_name,
    )

    server.add_insecure_port('[::]:' + str(GATEWAY_PORT))

    l.LOGGER('COMPUTE POWER RATE -> ' + str(COMPUTE_POWER_RATE))
    l.LOGGER('COST OF BUILD -> ' + str(COST_OF_BUILD))
    l.LOGGER('EXECUTION BENEFIT -> ' + str(EXECUTION_BENEFIT))
    l.LOGGER('IGNORE FATHER NETWORK ON SERVICE BALANCER -> ' + str(IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER))
    l.LOGGER('SEND ONLY HASHES ASKING COST -> ' + str(SEND_ONLY_HASHES_ASKING_COST))
    l.LOGGER('DENEGATE COST REQUEST IF DONT VE THE HASH -> ' + str(DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH))
    l.LOGGER('MANAGER ITERATION TIME-> ' + str(MANAGER_ITERATION_TIME))
    l.LOGGER('AVG COST MAX PROXIMITY FACTOR-> ' + str(COST_AVERAGE_VARIATION))
    l.LOGGER('GAS_COST_FACTOR-> ' + str(GAS_COST_FACTOR))
    l.LOGGER('MODIFY_SERVICE_SYSTEM_RESOURCES_COST_FACTOR-> ' + str(MODIFY_SERVICE_SYSTEM_RESOURCES_COST))

    l.LOGGER('Starting gateway at port' + str(GATEWAY_PORT))

    server.start()
    server.wait_for_termination()
