from typing import Generator

from bee_rpc import client as bee, buffer_pb2

from protos import celaut_pb2
from src.utils import activity_window
from src.utils.cost_functions.resource_availability import get_resource_availability
from src.utils.logger import LOGGER as logger


class GetResourceAvailabilityIterable:
    """Answers "could you run an instance shaped like this right now?".

    Unlike GetServiceEstimatedCost, the input is a bare resource profile that need not
    correspond to any packed service on either side -- there is no hash to look up and
    no registry round-trip. It is what a peer evaluating a
    Service.PossibleEnvironmentWorkload scenario asks, since a descendant workload
    group may declare only `resources`, with no `hash` or embedded `service`.

    The answer is `get_resource_availability`'s, verbatim: the same admission gate a
    real StartService goes through locally, so a peer is told exactly what this node
    would decide about itself and nothing more.
    """

    def __init__(self, request_iterator, context):
        self.parser_iterator = bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices=celaut_pb2.Service.Container.Resources,
            partitions_message_mode=True
        )
        self.context = context

    def __iter__(self) -> Generator[buffer_pb2.Buffer, None, None]:
        logger('Request for resource availability.')
        try:
            # An empty request is a well-formed question with a trivial answer ("can
            # you run something with no declared limits?"), so it is answered rather
            # than refused -- the same shape get_resource_availability itself gives an
            # unset `at_most`.
            resources = next(self.parser_iterator, celaut_pb2.Service.Container.Resources())
            if type(resources) is not celaut_pb2.Service.Container.Resources:
                logger(f'Resource availability asked with the wrong type: {type(resources)}.')
                resources = celaut_pb2.Service.Container.Resources()

            availability = get_resource_availability(resources)

            # Outside `activity_window` the answer is no, whatever the resources say.
            # A peer probing this node's capacity is asking whether it could place a
            # workload here, and after hours it could not -- reporting the room this
            # machine has would get the workload sent and then refused at launch.
            #
            # Overlaid here rather than inside `get_resource_availability` because that
            # function answers "does this shape fit?", which is a question about the
            # machine, and is also what the operator's own launches go through. The
            # hours belong to the door, not to the room.
            if not activity_window.is_open():
                availability = dict(
                    availability,
                    can_execute=False,
                    reason=activity_window.closed_reason(),
                )

            # No `indices`: the response is a single flat message, the same shape
            # StopService's Refund is serialized with. The caller pairs it with
            # `indices_parser=ResourceAvailability` + `partitions_message_mode_parser`.
            yield from bee.serialize_to_buffer(
                message_iterator=celaut_pb2.ResourceAvailability(
                    can_execute=availability["can_execute"],
                    reason=availability.get("reason", ""),
                )
            )
        finally:
            logger('End request for resource availability.')
