from typing import Generator
from bee_rpc import client as bee, buffer_pb2

from protos import celaut_pb2
from protos.gateway_bee import StartService_input_indices
from src.gateway.iterables.abstract_input_service_iterable import find_service_hash
from src.virtualizers.docker import build
from src.utils.logger import LOGGER as logger
from src.utils.utils import service_extended, read_metadata_from_disk


class GetServiceIterable:
    
    def __init__(self, request_iterator, context):
        self.parser_iterator = bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices=celaut_pb2.Metadata.HashTag.Hash,
            partitions_message_mode=True
        )
        self.context = context

    def __iter__(self) -> Generator[buffer_pb2.Buffer, None, None]:
        logger('Request for a service.')
        service_hash = None
        for hash in self.parser_iterator:
            if type(hash) is not celaut_pb2.Metadata.HashTag.Hash:
                logger(f'The hash provided has wrong type. {type(hash)}')
                continue
            _hash, _ = find_service_hash(hash)
            if _hash:
                service_hash = _hash
                break
            
        if not service_hash:
            logger("Any service hash on the request input.")
            return
            
        try:
            yield from bee.serialize_to_buffer(
                message_iterator=service_extended(
                    metadata=read_metadata_from_disk(service_hash=service_hash),
                    recursion_guard_token=None  # TODO: Needed if executing the same RPC to peers as well, in case the service is not available locally.
                ),
                indices=StartService_input_indices  # Client and configuration not needed.
            )
        except build.UnsupportedArchitectureException as e:
            raise e
        finally:
            logger("Finalized request for a service.")
