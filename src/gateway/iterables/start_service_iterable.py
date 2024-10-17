import os
from typing import Generator

from grpcbigbuffer import client as grpcbf, buffer_pb2

from src.gateway.iterables.abstract_service_iterable import AbstractServiceIterable
from src.gateway.launcher.launch_service import launch_service
from src.utils import logger as l
from src.utils.env import EnvManager
from src.utils.utils import get_only_the_ip_from_context, read_metadata_from_disk, read_service_from_disk
from src.utils.env import EnvManager

env_manager = EnvManager()

REGISTRY = env_manager.get_env("REGISTRY")

CONFIGURATION_REQUIRED = False  # TODO añadir como variable de entorno. Por si el nodo debe de ser mas estricto.


class StartServiceIterable(AbstractServiceIterable):

    def start(self):
        l.LOGGER('Starting service by ' + str(self.context.peer()) + ' ...')

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        if CONFIGURATION_REQUIRED and not self.configuration.config:
            raise Exception("Client or configuration ")

        l.LOGGER('Launch service with configuration')
        yield from grpcbf.serialize_to_buffer(
            indices={},
            message_iterator=launch_service(
                service=read_service_from_disk(service_hash=self.service_hash),
                metadata=self.metadata if self.metadata else read_metadata_from_disk(service_hash=self.service_hash),
                config=self.configuration,
                service_id=self.service_hash,
                father_ip=get_only_the_ip_from_context(context_peer=self.context.peer()),
                father_id=self.client_id,  # Only client, not set the internal_service_id because depends of the recursion guard.
                recursion_guard_token=self.recursion_guard_token
            )
        )

    def final(self):
        if not self.service_saved:
            l.LOGGER(
                f"\n"
                f"The service is not in the registry and the request does not have the definition.\n "
                f"Only has the service hash -> {self.service_hash} \n"
                f"And the metadata -> {self.metadata} \n"
                f"This is on registry -> {[h for h in os.listdir(REGISTRY)]} \n"
                f"\n"
            )
