import os
from typing import Generator

from grpcbigbuffer import client as grpcbf, buffer_pb2
from grpcbigbuffer.block_driver import WITHOUT_BLOCK_POINTERS_FILE_NAME

from protos import celaut_pb2 as celaut
from src.gateway.launch_service import launch_service
from src.gateway.iterables.service_iterable import ServiceIterable
from src.manager.resources_manager import mem_manager
from src.utils import logger as l
from src.utils.env import REGISTRY
from src.utils.utils import get_only_the_ip_from_context, read_file

CONFIGURATION_REQUIRED = False  # TODO añadir como variable de entorno. Por si el nodo debe de ser mas estricto.


def get_from_registry(service_hash: str) -> celaut.Service:
    l.LOGGER('Getting ' + service_hash + ' service from the local registry.')
    filename: str = REGISTRY + service_hash
    if not os.path.exists(filename):
        return None

    if os.path.isdir(filename):
        filename = filename + '/' + WITHOUT_BLOCK_POINTERS_FILE_NAME
    try:
        with mem_manager(2 * os.path.getsize(filename)) as iolock:
            service = celaut.Service()
            service.ParseFromString(read_file(filename=filename))
            return service
    except (IOError, FileNotFoundError):
        l.LOGGER('The service was not on registry.')
        return None


class StartServiceIterable(ServiceIterable):

    def start(self):
        l.LOGGER('Starting service by ' + str(self.context.peer()) + ' ...')

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        if CONFIGURATION_REQUIRED and not self.configuration:
            raise Exception("Client or configuration ")

        l.LOGGER('Launch service with configuration')
        yield from grpcbf.serialize_to_buffer(
            indices={},
            message_iterator=launch_service(
                service=get_from_registry(service_hash=self.service_hash),
                metadata=self.metadata,
                config=self.configuration,
                system_requirements=self.system_requeriments,
                max_sysreq=self.max_sysreq,
                initial_gas_amount=self.initial_gas_amount,
                service_id=self.service_hash,
                father_ip=get_only_the_ip_from_context(context_peer=self.context.peer()),
                father_id=self.client_id,
                recursion_guard_token=self.recursion_guard_token
            )
        )
        return

    def final(self):
        l.LOGGER('The service is not in the registry and the request does not have the definition.' \
                 + str([(h.type.hex(), h.value.hex()) for h in self.hashes]))
