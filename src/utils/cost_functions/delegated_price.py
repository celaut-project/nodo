"""What this node charges a delegated service, and what it must cover (#577).

A delegated instance is charged what this node advertises, as a local one is: the same
cost functions, its resources, our rates and our scarcity. Nothing of the peer reaches
the client. The margin of the delegation is the difference between that and what the
peer charges this node.

So the peers are not a price to pass on but a cost to cover. This module says whether a
peer is covered: its quote, in local MU, against our own price for the same service.
"""

from typing import Optional

from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost


def _mu(amount: celaut_pb2.Amount) -> int:
    return int(amount.n or 0)


def own_price(
        metadata: celaut.Metadata,
        configuration: celaut_pb2.Configuration,
        resources: celaut.Service.Container.Resources,
        arch: Optional[str] = None,
        service: Optional[celaut.Service] = None,
) -> Optional[celaut_pb2.EstimatedCost]:
    """The price of this node for the service, whether or not it has room for it.

    A node delegates mostly because it has no room, so the price that it advertises and
    charges cannot depend on the room.
    """
    return generate_estimated_cost(
        metadata=metadata, config=configuration, resources=resources,
        arch=arch, service=service, check_availability=False,
    )


def peer_build_mu(peer_cost: celaut_pb2.EstimatedCost, initial_mu: int) -> int:
    """The build that the peer quoted, in local MU.

    ``peer_cost`` is the quote of the peer, already in local MU. ``initial_mu`` is the
    balance that the quote asked for, in local MU. The quote is the build plus that
    balance (#563), so the build is what is left above it.
    """
    return max(_mu(peer_cost.cost) - max(int(initial_mu), 0), 0)


def covers(own: celaut_pb2.EstimatedCost, peer_cost: celaut_pb2.EstimatedCost) -> bool:
    """True if what this node charges covers what the peer would charge it.

    The start (the cost has the same balance in both, so this compares the builds), one
    maintenance period at the start, and one period at the most that the service may grow
    to: the service changes its resources on the peer, and this node charges it for them
    by its own rates (``maintain.measured_shape``). A peer that quoted no period is
    covered by it.
    """
    return _mu(own.cost) >= _mu(peer_cost.cost) \
        and _mu(own.init_maintenance_cost) >= _mu(peer_cost.init_maintenance_cost) \
        and _mu(own.max_maintenance_cost) >= _mu(peer_cost.max_maintenance_cost)
