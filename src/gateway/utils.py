import os
import shutil
from typing import Generator, List

import grpc
import netifaces as ni
from grpcbigbuffer import client as grpcbf

from src.utils.utils import get_ledger_and_contract_addr_from_contract
from contracts.vyper_gas_deposit_contract.interface \
    import CONTRACT_HASH as DEFAULT_PROVISIONAL_CONTRACT_HASH, CONTRACT as DEFAULT_PROVISIONAL_CONTRACT
from protos import celaut_pb2 as celaut, gateway_pb2, gateway_pb2_grpc
from protos.gateway_pb2_grpcbf import GetServiceTar_input
from src.utils import logger as l
from src.utils import utils as utils
from src.utils.env import GATEWAY_PORT, REGISTRY


def __generate_contract_ledger() -> Generator[celaut.Service.Api.ContractLedger, None, None]:
    for ledger, address in get_ledger_and_contract_addr_from_contract(DEFAULT_PROVISIONAL_CONTRACT_HASH):
        contract_ledger = celaut.Service.Api.ContractLedger()
        contract_ledger.contract = DEFAULT_PROVISIONAL_CONTRACT
        contract_ledger.ledger, contract_ledger.contract_addr = ledger, address
        yield contract_ledger


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

    instance.api.contract_ledger.extend(
        [e for e in __generate_contract_ledger()]
    )
    return gateway_pb2.Instance(
        instance=instance
    )


# If the service is not on the registry, save it.
def save_service(
        service_with_meta_dir: str,
        service_hash: str
) -> bool:
    if not os.path.isdir(REGISTRY + service_hash):
        try:
            shutil.move(service_with_meta_dir, REGISTRY + service_hash)
            return True
        except Exception as e:
            l.LOGGER('Exception saving a service: ' + str(e))
    return False


def search_container(
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
                    metadata=metadata
                ),
                indices_serializer=GetServiceTar_input
            ))
            break
        except Exception as e:
            l.LOGGER('Exception during search container process: ' + str(e))
            pass


def search_file(hashes: List[gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash], ignore_network: str = None) -> \
Generator[celaut.Any, None, None]:
    print('SEARCH FILE METHOD NOT IMPLEMENTED.')
    raise Exception('SEARCH FILE METHOD NOT IMPLEMENTED.')
    """
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
    """



def search_definition(hashes: List[gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash], ignore_network: str = None) \
        -> celaut.Service:
    l.LOGGER('SEARCH DEFINITION NOT IMPLEMENTED.')
    raise Exception('SEARCH DEFINITION NOT IMPLEMENTED.')

    """
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
                # save_service(
                #    service_p1=next(service_partitions_iterator),
                #    service_p2=next(service_partitions_iterator),
                #    metadata=any.metadata
                # )
                service = celaut.Service()
                service.ParseFromString(any.value)
                return service
    
        try:
            identifier = hashes[0].value.hex()
        except Exception:
            identifier = '__not_provided__'
    
        l.LOGGER('The service ' + identifier + ' was not found.')
        raise Exception('The service ' + identifier + ' was not found.')    
    """

