import itertools
import os
import shutil

import grpc
import grpcbigbuffer as grpcbf
import netifaces as ni
from typing import Generator

import src.manager.resources_manager as iobd
from contracts.eth_main.utils import get_ledger_and_contract_addr_from_contract
from protos import celaut_pb2 as celaut, gateway_pb2, gateway_pb2_grpc
from protos import gateway_pb2_grpcbf
from protos.gateway_pb2_grpcbf import GetServiceTar_input
from src.utils import logger as l
from src.utils import utils as utils
from src.utils.env import GATEWAY_PORT, REGISTRY
from src.utils.verify import check_service, get_service_hex_main_hash

from contracts.vyper_gas_deposit_contract.interface \
    import CONTRACT_HASH as DEFAULT_PROVISIONAL_CONTRACT_HASH, CONTRACT as DEFAULT_PROVISIONAL_CONTRACT


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

    try:
        identifier = hashes[0].value.hex()
    except: identifier = '__not_provided__'
    
    l.LOGGER('The service ' + identifier + ' was not found.')
    raise Exception('The service ' + identifier + ' was not found.')
