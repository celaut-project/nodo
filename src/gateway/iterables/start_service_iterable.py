import os
from typing import Generator, Optional

import grpc

from src.gateway.client_gate import require_caller
from src.gateway.iterables.abstract_input_service_iterable import AbstractInputServiceIterable
from src.gateway.launcher.launch_service import launch_service
from src.manager.manager import stop_instance
from src.utils import logger as log
from protos.gateway_bee import rpc_output
from src.utils.bee_client import BeeClient, Buffer
from src.database.sql_connection import SQLConnection
from protos import celaut_pb2
from src.utils.config import ConfigManager
from src.utils.utils import get_only_the_ip_from_context, read_metadata_from_disk, read_service_from_disk
from src.utils.config import ConfigManager
from src.utils.verify import get_service_hex_main_hash

env_manager = ConfigManager()

REGISTRY = env_manager.get("REGISTRY")

CONFIGURATION_REQUIRED = env_manager.get("CONFIGURATION_REQUIRED")  # In case the node needs to be stricter.


class StartServiceIterable(AbstractInputServiceIterable):

    # Who asks, for the StartReference: its client_id, or the local instance at its
    # address. Found once.
    _caller_id: Optional[str] = None

    def start(self):
        log.LOGGER('Starting service by ' + str(self.context.peer()) + ' ...')

    def _caller(self) -> Optional[str]:
        """The caller that a StartReference is keyed by: the father of the instance, as
        ``launch_service`` takes it. None while no Client arrived from a caller that is
        not a local instance."""
        if self.client_id:
            return self.client_id
        if self._caller_id is None:
            self._caller_id = SQLConnection().get_local_instance_id_by_uri(
                get_only_the_ip_from_context(context_peer=self.context.peer())) or ""
        return self._caller_id or None

    def _refuse(self, code: grpc.StatusCode, message: str):
        self.context.abort(code, message)
        raise Exception(message)

    def _answer(self, known: tuple) -> Generator[Buffer, None, None]:
        """Answer from what the first request with this reference did (#578). Nothing is
        started, and nothing is charged."""
        state, instance_bytes, error = known
        self.answered = True
        reference = self.start_ref.hex()[:16]
        if state == 'running':
            self._refuse(grpc.StatusCode.ABORTED,
                         f"The start {reference} is in progress. Ask again later.")
        if state != 'started':
            self._refuse(grpc.StatusCode.FAILED_PRECONDITION,
                         f"The start {reference} failed: {error}")
        instance = celaut_pb2.ServiceInstance()
        instance.ParseFromString(instance_bytes)
        log.LOGGER(f'The start {reference} was asked again: the same instance.')
        yield Buffer(signal=True)
        yield from BeeClient.respond(
            indices=rpc_output("StartService"),
            message_iterator=instance,
        )

    def repeat(self) -> Generator[Buffer, None, None]:
        if not self.start_ref:
            return
        caller = self._caller()
        if not caller:
            return
        known = SQLConnection().get_start_ref(caller, self.start_ref.hex())
        if known is None:
            return
        if not self._caller_checked:
            self._caller_checked = True
            require_caller(self.context, self.client_id or "")
        yield from self._answer(known)

    def generate(self) -> Generator[Buffer, None, None]:
        caller = self._caller()
        reference = self.start_ref.hex() if self.start_ref and caller else None
        if reference and not SQLConnection().claim_start_ref(caller, reference):
            # Another request with this reference came first.
            known = SQLConnection().get_start_ref(caller, reference)
            if known is not None:
                yield from self._answer(known)
                return
            reference = None
        try:
            instance = self._launch(reference)
        except Exception as e:
            if reference:
                SQLConnection().fail_start_ref(caller, reference, str(e))
            raise
        if reference:
            SQLConnection().finish_start_ref(
                caller, reference, instance.SerializeToString(), instance.token)

        yield from BeeClient.respond(
            indices=rpc_output("StartService"),
            message_iterator=instance,
        )

    def _launch(self, reference: Optional[str]) -> celaut_pb2.ServiceInstance:
        if CONFIGURATION_REQUIRED and not self.configuration:
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

        instance = launch_service(
            service_id=self.service_hash,
            service=service,
            metadata=metadata,
            configuration=self.configuration,
            father_ip=get_only_the_ip_from_context(context_peer=self.context.peer()),
            father_id=self.client_id,  # Only client, not set the internal_service_id because depends of the recursion guard.
            recursion_guard_token=self.recursion_guard_token,
            recursion_guard_hops=self.recursion_guard_hops,
            start_ref=reference,
        )

        # The launch starts in the middle of the request stream and takes some
        # seconds. If the call ended in that time (the client cancelled it, its
        # request stream failed, or its deadline passed), the answer goes to
        # nobody. The caller then has no token for the instance and cannot stop
        # it, but it paid for it. Stop it here, and give the deposit back (#487).
        # With a StartReference the caller can ask for it again (#578), so it is kept.
        if not reference and not self.context.is_active():
            log.LOGGER(
                f"The StartService call for {self.service_hash} ended before the answer. "
                f"Stop the instance that nobody received."
            )
            stop_instance(token=instance.token)
            raise Exception(
                f"The call ended before the instance of {self.service_hash} was sent. "
                f"The instance is stopped."
            )
        return instance

    def final(self):
        if self.answered:
            return
        if not self.service_saved:
            log.LOGGER(
                f"\n"
                f"The service is not in the registry and the request does not have the definition.\n "
                f"Only has the service hash -> {self.service_hash} \n"
                f"And the metadata -> {self.metadata} \n"
                f"This is on registry -> {[h for h in os.listdir(REGISTRY)]} \n"
                f"\n"
            )
        elif not self._caller_checked:
            # The service was ready but no client_id ever arrived: the gate in the base
            # class deferred, waiting for one, and the stream ended first. Its final()
            # makes that refusal definitive; overriding it without doing the same left
            # such a caller with an empty response instead of an error. A caller
            # already checked is not checked again, also when generate() failed.
            require_caller(self.context, self.client_id or "")
