"""What this node charges a delegated service, and what it must cover (#577).

A delegated instance is charged what this node advertises, as a local one is: our cost
functions, its resources and our rates. Nothing of the peer reaches the client. The
margin of the delegation is the difference between that and what the peer charges this
node.

Two things are not as for a local instance, because the load of the instance is on the
peer and not here:

* it is never free: ``free_tier`` gives away the capacity of this node, and a delegated
  instance uses none of it, while the peer charges this node for it;
* it is priced at the scarcity of its launch for its whole life
  (``execution_cost.charge_at_launch_price``): the scarcity of this node later says
  nothing about it.

So the peers are not a price to pass on but a cost to cover. ``covers`` says whether a
peer is covered: its quote, in local MU, against our own price for the same service.
"""

from typing import Mapping, Optional, Tuple

from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils.config import ConfigManager
from src.utils.cost_functions.execution_cost import (
    build_charge_mu, charge_at_launch_price, launch_price_bp, system_scarcity,
)
from src.utils.cost_functions.generate_estimated_cost import _service_hash_or_none
from src.utils.monetary import prices
from src.utils.utils import from_amount, to_amount
from src.virtualizers.architecture import UnsupportedArchitectureException
from src.virtualizers.interface import resolve_billable_resources

env_manager = ConfigManager()


def _mu(amount: celaut_pb2.Amount) -> int:
    return int(amount.n or 0)


def own_build_mu(metadata: celaut.Metadata) -> int:
    """The build that this node charges a delegated start, in MU.

    The build of a local start, without the free tier. A service that this node cannot
    build (an architecture that it does not have) is charged the build all the same: the
    client pays what this node advertises, whoever builds it.
    """
    try:
        return int(build_charge_mu(metadata=metadata))
    except UnsupportedArchitectureException:
        return int(prices().build_mu)


def billable_shapes(
        resources: celaut.Service.Container.Resources,
        service_hash: Optional[str] = None,
        service: Optional[celaut.Service] = None,
) -> Tuple[celaut.Sysresources, celaut.Sysresources]:
    """The resources that a delegated instance is charged for at the start, and the most.

    With the floors of this node (``resolve_billable_resources``), as a local instance:
    a manifest that declares no ``at_init`` is charged the floors, not nothing. The most
    is the ``at_most`` of the manifest, or the start when it declares none.
    """
    start = resolve_billable_resources(
        resources.at_init if resources.HasField("at_init") else celaut.Sysresources(),
        service_hash, service=service)
    most = resolve_billable_resources(resources.at_most, service_hash, service=service) \
        if resources.HasField("at_most") else start
    return start, most


def own_price(
        metadata: celaut.Metadata,
        configuration: celaut_pb2.Configuration,
        resources: celaut.Service.Container.Resources,
        arch: Optional[str] = None,
        service: Optional[celaut.Service] = None,
        launch_bp: Optional[Mapping[str, int]] = None,
) -> celaut_pb2.EstimatedCost:
    """The price of this node for running the service on a peer, whether or not it has room.

    The least ``initial_mu`` (the build and the balance that ``configuration`` asks
    for), and one maintenance period at the start and at the most, at ``launch_bp``:
    the multipliers of a launch now when not given. These are the figures that the ticks
    of a delegated instance charge (``maintain.settle_child``).
    """
    launch_bp = launch_bp or launch_price_bp(system_scarcity())
    seconds = int(env_manager.get("MANAGER_ITERATION_TIME"))
    start, most = billable_shapes(resources, _service_hash_or_none(metadata), service)
    initial_mu = from_amount(configuration.initial_mu) \
        if configuration and configuration.HasField("initial_mu") else 0
    return celaut_pb2.EstimatedCost(
        cost=to_amount(own_build_mu(metadata) + initial_mu),
        init_maintenance_cost=to_amount(charge_at_launch_price(start, seconds, launch_bp, arch)),
        max_maintenance_cost=to_amount(charge_at_launch_price(most, seconds, launch_bp, arch)),
        maintenance_seconds_loop=seconds,
    )


def priced_shape(
        service: Optional[celaut.Service],
        metadata: celaut.Metadata,
        service_id: Optional[str],
        arch: Optional[str],
) -> dict:
    """What the tick charges a delegated instance by, for the columns of its row.

    The resources that it holds (``mem_limit``, ``disk_space``, ``cpu_period``,
    ``cpu_quota``), the same at the start (``base_*``, which the resources that the peer
    shows are measured from, ``maintain.measured_shape``), the most that it is charged for
    (``max_*``), the architecture that selects the memory price and the multipliers of
    the launch (``launch_bp``). Empty without the service.
    """
    if service is None:
        return {}
    start, most = billable_shapes(service.container.resources, service_id, service)
    shape = {"arch": arch, "launch_bp": launch_price_bp(system_scarcity())}
    for key in ("mem_limit", "disk_space", "cpu_period", "cpu_quota"):
        shape[key] = shape[f"base_{key}"] = int(getattr(start, key))
        shape[f"max_{key}"] = int(getattr(most, key))
    return shape


def peer_build_mu(peer_cost: celaut_pb2.EstimatedCost, initial_mu: int) -> int:
    """The build that the peer quoted, in local MU.

    ``peer_cost`` is the quote of the peer, already in local MU. ``initial_mu`` is the
    balance that the quote asked for, in local MU. The quote is the build plus that
    balance (#563), so the build is what is left above it.
    """
    return max(_mu(peer_cost.cost) - max(int(initial_mu), 0), 0)


def _per_second_at_least(own_mu: int, own_seconds: int, peer_mu: int, peer_seconds: int) -> bool:
    """``own_mu`` per ``own_seconds`` is at least ``peer_mu`` per ``peer_seconds``.

    Each node prices one period of its own ``MANAGER_ITERATION_TIME``. A period that does
    not say its length is compared as it is.
    """
    if own_seconds > 0 and peer_seconds > 0:
        return own_mu * peer_seconds >= peer_mu * own_seconds
    return own_mu >= peer_mu


def covers(own: celaut_pb2.EstimatedCost, peer_cost: celaut_pb2.EstimatedCost) -> bool:
    """True if what this node charges covers what the peer would charge it.

    The start (the cost has the same balance in both, so this compares the builds), and
    the maintenance per second at the start and at the most that the service may grow
    to: the service changes its resources on the peer, and this node charges it for them
    by its own rates (``maintain.measured_shape``). A peer that quoted no period is
    covered by it.
    """
    own_s, peer_s = int(own.maintenance_seconds_loop), int(peer_cost.maintenance_seconds_loop)
    return _mu(own.cost) >= _mu(peer_cost.cost) \
        and _per_second_at_least(_mu(own.init_maintenance_cost), own_s,
                                 _mu(peer_cost.init_maintenance_cost), peer_s) \
        and _per_second_at_least(_mu(own.max_maintenance_cost), own_s,
                                 _mu(peer_cost.max_maintenance_cost), peer_s)
