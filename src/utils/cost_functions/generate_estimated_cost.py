from typing import Optional
from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils.cost_functions.resource_availability import get_resource_availability
from src.utils.cost_functions.general_cost_functions import compute_start_service_cost, compute_maintenance_cost
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
) -> Optional[celaut_pb2.EstimatedCost]:
    """What running this service here would cost, at the current prices and load.

    ``arch`` is the service's architecture. It selects the memory price when the
    operator has set one per architecture, so a quote and the charge the maintenance
    tick then levies come from the same rate -- quoting the scalar and charging a
    per-arch rate would have the node bill above what it offered. Omitted, both sides
    use the scalar price, which is what a node with no per-arch pricing does.
    """

    initial_mu = from_amount(config.initial_mu) \
        if config and config.HasField("initial_mu") else 0

    if not get_resource_availability(resources=resources)["can_execute"]:
        return

    # What the client pays up front: the one-off build, plus the balance the instance
    # starts with. That balance is what buys the runtime window (it is derived from the
    # requested resources by `default_initial_balance`), so the window is not also
    # charged here -- doing both billed it twice.
    initial_cost_mu = compute_start_service_cost(
        metadata=metadata,
        initial_balance_mu=initial_mu,
    )

    # Both maintenance figures are quoted for what the instance will *hold*, not for
    # what its manifest asks for. The virtualizer raises anything below a floor when it
    # creates the guest -- undeclared CPU becomes one vCPU, RAM below MIN_MEM_MIB is
    # raised to it, the rootfs image is at least MIN_ROOTFS_BYTES -- and the tick then
    # charges the resolved row, so a quote has to describe that same shape.
    service_hash = _service_hash_or_none(metadata)

    # Calculate estimated cost for local execution.
    return celaut.EstimatedCost(

        # Initial cost.
        cost=to_amount(initial_cost_mu),

        # Maintenance cost per manager iteration, at the resources it starts with.
        init_maintenance_cost=to_amount(compute_maintenance_cost(
            system_resources=resolve_billable_resources(resources.at_init, service_hash),
            seconds=MANAGER_ITERATION_TIME,
            arch=arch,
        )) if resources.HasField('at_init') else to_amount(0),

        # Maintenance cost per manager iteration, at the most it may grow to.
        max_maintenance_cost=to_amount(compute_maintenance_cost(
            system_resources=resolve_billable_resources(resources.at_most, service_hash),
            seconds=MANAGER_ITERATION_TIME,
            arch=arch,
        )) if resources.HasField('at_most') else to_amount(0),
        
        # Maintenance frecuency in seconds.
        maintenance_seconds_loop=MANAGER_ITERATION_TIME,
        
        # Variance of the three costs.
        variance=0,  # TODO compute_variance
    )
