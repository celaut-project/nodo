from typing import Generator, Optional, Tuple

from protos import celaut_pb2
from protos.gateway_bee import GetResourceAvailability_input_indices
from src.gateway.client_gate import require_caller, simple_rpc_timeout_seconds
from src.utils import activity_window
from src.utils.bee_client import BeeClient, Buffer
from src.utils.cost_functions.resource_availability import get_architecture_availability
from src.utils.logger import LOGGER as logger
from src.utils.tools.recursion_guard import RecursionGuard, received_hops


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

    The request may carry a ``RecursionGuard`` (index 3, #456), validated and held the
    way StartService holds one: a token this node is already answering is refused, one
    with no hops left is refused, and a missing one makes this request a root. The
    answer asks nobody else today, so there is nothing to forward the guard to yet. It
    is held anyway so that the day this RPC also probes peers, the probe goes out from
    inside the guard as ``check_resource_availability_on_peer(peer, request,
    recursion_guard_token=self.recursion_guard_token)`` and the cycle and depth limits
    apply to it with no further change to the wire.
    """

    def __init__(self, request_iterator, context):
        self.request_iterator = request_iterator
        self.context = context
        # The token in force while answering: the caller's, or the root one minted
        # here. None outside the guard.
        self.recursion_guard_token: Optional[str] = None

    def _parse(self) -> Tuple[Optional[celaut_pb2.ArchitectureResources], str, Optional[str], Optional[int]]:
        """``(request, client_id, token, hops)``: what ``parse_with_client`` returns,
        plus the guard. Drains the stream for the same reason that function does --
        order on the wire is the caller's choice, and nothing may be left unread for a
        later ``next()`` to hang on."""
        request, client_id, token, hops = None, "", None, None
        for r in BeeClient.parse(
                self.request_iterator,
                indices=GetResourceAvailability_input_indices,
                timeout=simple_rpc_timeout_seconds(),
        ):
            if isinstance(r, celaut_pb2.Client):
                client_id = r.client_id
            elif isinstance(r, celaut_pb2.RecursionGuard):
                token, hops = r.token, received_hops(r)
            elif isinstance(r, celaut_pb2.ArchitectureResources):
                request = r
        return request, client_id, token, hops

    def __iter__(self) -> Generator[Buffer, None, None]:
        logger('Request for resource availability.')
        try:
            # An empty request is a well-formed question with a trivial answer ("can
            # you run something with no declared limits?"), so it is answered rather
            # than refused -- the same shape get_resource_availability itself gives an
            # unset `at_most`.
            request, client_id, token, hops = self._parse()
            require_caller(self.context, client_id)
            if request is None:
                request = celaut_pb2.ArchitectureResources()

            # After the client gate, so a caller this node does not know can neither
            # park a token in its registry nor learn whether one is held.
            with RecursionGuard(
                    token=token,
                    generate=True,
                    remaining_hops=hops,
            ) as self.recursion_guard_token:
                yield from self._answer(request)
        finally:
            self.recursion_guard_token = None
            logger('End request for resource availability.')

    def _answer(self, request: celaut_pb2.ArchitectureResources) -> Generator[Buffer, None, None]:
        availability = get_architecture_availability(request)

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
