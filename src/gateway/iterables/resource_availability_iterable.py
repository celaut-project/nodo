from typing import Generator

from protos import celaut_pb2
from src.gateway.client_gate import parse_with_client, require_caller
from src.utils import activity_window
from src.utils.bee_client import BeeClient, Buffer
from src.utils.cost_functions.resource_availability import get_architecture_availability
from src.utils.logger import LOGGER as logger
from src.utils.tools.query_cache import QueryCache, availability_ttl, canonical_key


class GetResourceAvailabilityIterable:
    """Answers "could you run an instance of this architecture, shaped like this, right now?".

    Unlike GetServiceEstimatedCost, the input is a bare resource profile that need not
    correspond to any packed service on either side -- there is no hash to look up and
    no registry round-trip. It is what a peer evaluating a
    Service.PossibleEnvironmentWorkload scenario asks, since a descendant workload
    group may declare only `resources`, with no `hash` or embedded `service`.

    The answer is `get_architecture_availability`'s, verbatim: the same admission gate a
    real StartService goes through locally, so a peer is told exactly what this node
    would decide about itself and nothing more.

    The answer depends only on the question, so it is remembered by the content of the
    question for `network.QUERY_CACHE_AVAILABILITY_TTL_SECONDS` (#456), and one asked
    while it is being computed waits for that answer. Availability moves fast, which is
    why that TTL is short, and every local start, stop or resize forgets these answers
    at once; 0 turns the cache off. See `src/utils/tools/query_cache.py`.
    """

    def __init__(self, request_iterator, context):
        self.request_iterator = request_iterator
        self.context = context

    def __iter__(self) -> Generator[Buffer, None, None]:
        logger('Request for resource availability.')
        try:
            # An empty request is a well-formed question with a trivial answer ("can
            # you run something with no declared limits?"), so it is answered rather
            # than refused -- the same shape get_resource_availability itself gives an
            # unset `at_most`.
            request, client_id = parse_with_client(
                self.request_iterator, method="GetResourceAvailability"
            )
            require_caller(self.context, client_id)
            if request is None:
                request = celaut_pb2.ArchitectureResources()

            availability = QueryCache().get_or_compute(
                canonical_key("GetResourceAvailability", request),
                availability_ttl(),
                lambda: get_architecture_availability(request),
            )

            # After the cache, so the hours always win over a remembered answer.
            #
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
            yield from BeeClient.respond(
                message_iterator=celaut_pb2.ResourceAvailability(
                    can_execute=availability["can_execute"],
                    reason=availability.get("reason", ""),
                )
            )
        finally:
            logger('End request for resource availability.')
