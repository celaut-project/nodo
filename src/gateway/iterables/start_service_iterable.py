import os
from typing import Generator

from bee_rpc import client as bee, buffer_pb2

from src.gateway.iterables.abstract_input_service_iterable import AbstractInputServiceIterable
from src.gateway.launcher.launch_service import launch_service
from src.utils import logger as log
from src.utils.env import EnvManager
from src.utils.utils import get_only_the_ip_from_context, read_metadata_from_disk, read_service_from_disk
from src.utils.env import EnvManager
from src.utils.verify import get_service_hex_main_hash

env_manager = EnvManager()

REGISTRY = env_manager.get_env("REGISTRY")

CONFIGURATION_REQUIRED = env_manager.get_env("CONFIGURATION_REQUIRED")  # In case the node needs to be stricter.


class StartServiceIterable(AbstractInputServiceIterable):

    def start(self):
        log.LOGGER('Starting service by ' + str(self.context.peer()) + ' ...')

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        if CONFIGURATION_REQUIRED and not self.configuration.config:
            raise Exception("Client or configuration ")
        
        service = read_service_from_disk(service_hash=self.service_hash)
        if not service:
            raise Exception(f"No service {self.service_hash} on registry")

        log.LOGGER(f'Launch service {self.service_hash}')

        metadata = self.metadata if self.metadata else read_metadata_from_disk(service_hash=self.service_hash)
        if not metadata:
            raise Exception(f"No metadata for the service {self.metadata} on registry")

        service_id = get_service_hex_main_hash(metadata=metadata)
        if service_id != self.service_hash:
            log.LOGGER(f'There is some problem with the metadata {service_id} != {self.service_hash}')
            for hash in list(metadata.hashtag.hash):
                log.LOGGER(f"-  {hash.type.hex()}: {hash.value.hex()}")
            raise Exception(f"Corrupt metadata for the service {self.service_hash}")

        yield from bee.serialize_to_buffer(
            indices={},  # Why indices are not set?  Because StartService returns only one element, an instance.
            message_iterator=launch_service(
                service_id=self.service_hash,
                service=service,
                metadata=metadata,
                configuration=self.configuration,
                father_ip=get_only_the_ip_from_context(context_peer=self.context.peer()),
                father_id=self.client_id,  # Only client, not set the internal_service_id because depends of the recursion guard.
                recursion_guard_token=self.recursion_guard_token
            )
        )

    def final(self):
        if not self.service_saved:
            log.LOGGER(
                f"\n"
                f"The service is not in the registry and the request does not have the definition.\n "
                f"Only has the service hash -> {self.service_hash} \n"
                f"And the metadata -> {self.metadata} \n"
                f"This is on registry -> {[h for h in os.listdir(REGISTRY)]} \n"
                f"\n"
            )
