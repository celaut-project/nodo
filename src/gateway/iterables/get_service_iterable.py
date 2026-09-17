from typing import Generator
from bee_rpc import client as bee, buffer_pb2
from bee_rpc.control import StreamControl
from bee_rpc.utils import get_expanded_block_length

from protos import celaut_pb2
from protos.gateway_bee import StartService_input_indices
from src.gateway.iterables.abstract_input_service_iterable import find_service_hash
from src.virtualizers.architecture import UnsupportedArchitectureException
from src.utils.logger import LOGGER as logger
from src.utils.utils import service_extended, read_metadata_from_disk


class GetServiceIterable:

    def __init__(self, request_iterator, context):
        # Shared with the response's serialize_to_buffer below: the parse side
        # queues a skip request the moment it meets the start of a block we
        # already hold, and the serialize side is what actually stops sending
        # one -- see bee_rpc.control.StreamControl and issue #371.
        self.control = StreamControl()
        self.parser_iterator = bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices=celaut_pb2.Metadata.HashTag.Hash,
            partitions_message_mode=True,
            control=self.control,
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

        # Our side of the request is fully parsed, but the peer's skip requests
        # -- naming blocks it already holds -- only start arriving once it
        # begins parsing what we are about to send below. Keep reading the
        # request direction in the background for exactly those, the way
        # StreamControl.watch() is meant to be used from a server handler.
        self.control.watch()

        try:
            yield from bee.serialize_to_buffer(
                message_iterator=service_extended(
                    metadata=read_metadata_from_disk(service_hash=service_hash),
                    recursion_guard_token=None  # TODO: Needed if executing the same RPC to peers as well, in case the service is not available locally.
                ),
                indices=StartService_input_indices,  # Client and configuration not needed.
                control=self.control,
            )
        except UnsupportedArchitectureException as e:
            raise e
        finally:
            # `_skip` is what the peer asked us to skip: blocks it already held,
            # so their bodies never left this node. Reported here because
            # dedup that saves nothing visible is dedup an operator has no way
            # to notice is (or isn't) working.
            skipped = self.control._skip
            if skipped:
                saved = sum(get_expanded_block_length(block_id) for block_id in skipped)
                logger(
                    f"Skipped {len(skipped)} block(s) already held by the peer "
                    f"({saved} bytes)."
                )
            logger("Finalized request for a service.")
