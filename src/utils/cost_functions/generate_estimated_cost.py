from typing import Optional
from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils.cost_functions.resource_availability import availability_reclaiming_disk
from src.utils.cost_functions.start_funding import initial_children_mu
from src.utils.cost_functions import price_bound
from src.utils.cost_functions.execution_cost import launch_charge_mu
from src.utils.cost_functions.general_cost_functions import compute_start_service_cost
from src.utils.utils import from_amount, to_amount
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger
from src.utils.verify import get_service_hex_main_hash
from src.virtualizers.interface import resolve_billable_resources

env_manager = ConfigManager()
MANAGER_ITERATION_TIME = env_manager.get("MANAGER_ITERATION_TIME")


def _service_hash_or_none(metadata: celaut.Metadata) -> Optional[str]:
    """The hash to look the built image up by, or None if metadata does not carry one.

    Only ever refines the quote (an already-built service prices its real image
    instead of the floor), so an unreadable hash falls back rather than failing the
    quote -- the caller's next step is `build_charge_mu`, which reports it properly.
    """
    try:
        return get_service_hex_main_hash(metadata=metadata)
    except Exception as e:
        logger(f"[PRICING] Could not read the service hash from metadata ({e}); "
               "quoting disk at its floor.")
        return None


def generate_estimated_cost(
        metadata: celaut.Metadata,
        config: celaut.Configuration,
        resources: celaut.Service.Container.Resources,
        arch: Optional[str] = None,
        service: Optional[celaut.Service] = None,
        check_availability: bool = True,
) -> Optional[celaut_pb2.EstimatedCost]:
    """What running this service here would cost, at the current prices and load.

    ``arch`` is the service's architecture. It selects which of this node's benchmark
    scores admission holds the service's required ones against, and the memory price when the
    operator has set one per architecture, so a quote and the charge the maintenance
    tick then levies come from the same rate -- quoting the scalar and charging a
    per-arch rate would have the node bill above what it offered. Omitted, both sides
    use the scalar price, which is what a node with no per-arch pricing does.

    ``service`` is the manifest, where the caller holds it. ``cost`` includes what the
    instance pays to start the children of its ``initial_environment_workload``
    (``start_funding.initial_children_mu``, #538). Without the manifest it is 0. Disk reads it too, only to notice a ``read_mode=ro``
    declaration: such an instance holds its image
    and no writable capacity at all, so the rootfs floors describe nothing it will
    be given and pricing it at them would quote a small immutable capsule at the
    128 MiB MIN_ROOTFS_BYTES. Omitted, the floors stay -- which over-quotes rather
    than under-quotes, and only over-quoting is recoverable.

    ``check_availability=False`` prices the service whatever the current load, for a
    caller that remembers the price and checks availability itself on every answer.
    """

    initial_mu = from_amount(config.initial_mu) \
        if config and config.HasField("initial_mu") else 0

    # Only the launcher checks here (the balancer's local candidate, its preflight), so a
    # refusal for disk alone may free the disk of suspended instances nobody pays for.
    if check_availability and not availability_reclaiming_disk(resources=resources, arch=arch)["can_execute"]:
        return

    # The least `initial_mu` that starts the service: the one-off build, plus the balance
    # the instance has after it (#563). The caller pays `initial_mu` and nothing else, and
    # this node takes the build out of it, so this figure is not a second charge on top
    # of the balance. The balance is what buys the runtime window (it is derived from the
    # requested resources by `default_initial_balance`), so the window is not also
    # charged here -- doing both billed it twice.
    initial_cost_mu = compute_start_service_cost(
        metadata=metadata,
        initial_balance_mu=initial_mu,
    )
    # The instance pays the `child_start` of its initial children out of its balance
    # (#538), so the least `initial_mu` that starts it includes them. `initial_mu` is the
    # balance of the service alone, and this is on top of it. A free node prices them
    # at 0, as it does the build and the balance.
    initial_cost_mu += initial_children_mu(service)

    # Both maintenance figures are quoted for what the instance will *hold*, not for
    # what its manifest asks for. The virtualizer raises anything below a floor when it
    # creates the guest -- undeclared CPU becomes one vCPU, RAM below MIN_MEM_MIB is
    # raised to it, the rootfs image is at least MIN_ROOTFS_BYTES -- and the tick then
    # charges the resolved row, so a quote has to describe that same shape.
    service_hash = _service_hash_or_none(metadata)

    # The price bound of the quote (#636), read once. The maintenance figures are the
    # price at the time of the estimate, at the launch price of now (``P``); the bound is
    # ``P * (1 + v)^k``.
    bound = price_bound.current_bound()

    # Calculate estimated cost for local execution.
    return celaut.EstimatedCost(

        # Initial cost: the build, the balance and the starts of the initial children.
        cost=to_amount(initial_cost_mu),

        # Maintenance cost per manager iteration, at the resources it starts with.
        init_maintenance_cost=to_amount(launch_charge_mu(
            system_resources=resolve_billable_resources(
                resources.at_init, service_hash, service=service
            ),
            seconds=MANAGER_ITERATION_TIME,
            arch=arch,
        )) if resources.HasField('at_init') else to_amount(0),

        # Maintenance cost per manager iteration, at the most it may grow to, at the same
        # price. Not a limit: the limit is this figure times (1 + v)^k when the quote is
        # bounded, and the ticks hold to it (`execution_cost.maintenance_charge_mu`).
        max_maintenance_cost=to_amount(launch_charge_mu(
            system_resources=resolve_billable_resources(
                resources.at_most, service_hash, service=service
            ),
            seconds=MANAGER_ITERATION_TIME,
            arch=arch,
        )) if resources.HasField('at_most') else to_amount(0),

        # Maintenance frecuency in seconds.
        maintenance_seconds_loop=MANAGER_ITERATION_TIME,

        # price_bounded, price_volatility and volatility_period_seconds.
        **price_bound.to_fields(bound),
    )
