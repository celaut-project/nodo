from typing import Generator
from bee_rpc.utils import get_expanded_block_length

from protos import celaut_pb2
from protos.gateway_bee import StartService_input_indices, rpc_input
from src.gateway.client_gate import require_caller, simple_rpc_timeout_seconds
from src.gateway.iterables.abstract_input_service_iterable import find_service_hash
from src.virtualizers.architecture import UnsupportedArchitectureException
from src.utils.bee_client import BeeClient, Buffer, StreamControl
from src.utils.logger import LOGGER as logger
from src.utils.utils import service_extended, read_metadata_from_disk


class GetServiceIterable:

    def __init__(self, request_iterator, context):
        # Shared with the response's serialize_to_buffer below: the parse side
        # queues a skip request the moment it meets the start of a block we
        # already hold, and the serialize side is what actually stops sending
        # one -- see bee_rpc.control.StreamControl and issue #371.
        self.control = StreamControl()
        # Bounded, not the unbounded drain a fully order-independent parse would
        # otherwise need: the request direction is never drained by this iterable
        # (block-skip keeps it open afterwards for the peer's later skip requests,
        # control.watch() below), so waiting on a message with no upper bound would
        # block a server thread forever the moment a caller sends less than it
        # declared. A generous, configurable timeout is what makes waiting for
        # *both* the Client and the Hash -- in whichever order the caller sent them
        # -- safe: see ``client_gate.simple_rpc_timeout_seconds``.
        self.parser_iterator = BeeClient.parse(
            request_iterator,
            indices=rpc_input("GetService"),
            control=self.control,
            timeout=simple_rpc_timeout_seconds(),
        )
        self.context = context

    def __iter__(self) -> Generator[Buffer, None, None]:
        logger('Request for a service.')
        service_hash = None
        client_id = ""
        # `client_seen`, not `client_id` itself, is what the break below waits on:
        # a Client can legitimately carry client_id="" (an unauthenticated caller
        # exempted some other way), and that empty string is a real answer, not
        # "not sent yet". Order-independent: whichever of the two messages arrives
        # first, this stops once both have. A caller that omits the Client
        # entirely (the "absent, not empty" convention every other RPC here uses --
        # a *sent* empty Client would not even survive the wire, since protobuf
        # serializes an all-default message to zero bytes and bee_rpc treats that
        # as nothing having arrived) is indistinguishable, up front, from one whose
        # Client is simply running behind the Hash -- both look like "no second
        # message yet". ``TimeoutError`` is what tells them apart: once it fires,
        # there is nothing more to wait for, so this proceeds with whatever was
        # collected (``client_id=""`` if no Client ever showed) rather than
        # refusing outright -- ``require_caller`` below is still the one deciding
        # whether that is enough.
        client_seen = False
        try:
            for r in self.parser_iterator:
                if type(r) is celaut_pb2.Client:
                    client_id = r.client_id
                    client_seen = True
                elif type(r) is celaut_pb2.Metadata.HashTag.Hash:
                    _hash, _ = find_service_hash(r)
                    if _hash:
                        service_hash = _hash
                else:
                    logger(f'The hash provided has wrong type. {type(r)}')
                    continue
                if service_hash and client_seen:
                    break
        except TimeoutError as e:
            logger(f"Gave up waiting for the rest of a GetService request: {e}")

        require_caller(self.context, client_id)

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
            yield from BeeClient.respond(
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
