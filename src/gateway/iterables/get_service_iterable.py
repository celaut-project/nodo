from typing import Generator

from grpcbigbuffer import client as grpcbf, buffer_pb2

from protos.gateway_pb2_grpcbf import StartService_input_indices
from src.builder import build
from src.gateway.iterables.abstract_service_iterable import AbstractServiceIterable, BreakIteration
from src.utils.logger import LOGGER as log
from src.utils.utils import service_extended, read_metadata_from_disk


class GetServiceIterable(AbstractServiceIterable):
    def start(self):
        log('Request for a service.')

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        try:
            yield from grpcbf.serialize_to_buffer(
                message_iterator=service_extended(
                    metadata=read_metadata_from_disk(service_hash=self.service_hash) if not self.metadata else self.metadata,
                    recursion_guard_token=self.recursion_guard_token
                ),
                indices=StartService_input_indices  # Client and configuration not needed.
            )
        except build.UnsupportedArchitectureException as e:
            raise e
        finally:
            yield buffer_pb2.Buffer(signal=True)
            raise BreakIteration
