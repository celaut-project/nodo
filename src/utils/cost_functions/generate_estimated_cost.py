from typing import Optional
from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils.cost_functions.resource_availability import availability_reclaiming_disk
from src.utils import activity_window, notice, on_close
from src.utils.cost_functions import price_bound
from src.utils.cost_functions.execution_cost import launch_charge_mu
from src.utils.cost_functions.general_cost_functions import compute_start_service_cost
from src.utils.utils import to_amount
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


def _enforce_devices(service) -> None:
    from src.devices import admission as devices_admission

    if not devices_admission.has_devices(service):
        return
    from src.virtualizers.selection import select_virtualizer

    devices_admission.enforce_supported(service, select_virtualizer(service))


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

    ``service`` is the manifest, where the caller holds it. ``cost`` includes the builds
    of the children of its ``initial_environment_workload``
    (``start_funding.initial_children_mu``, #538). Without the manifest they are 0. Disk reads it too, only to notice a ``read_mode=ro``
    declaration: such an instance holds its image
    and no writable capacity at all, so the rootfs floors describe nothing it will
    be given and pricing it at them would quote a small immutable capsule at the
    128 MiB MIN_ROOTFS_BYTES. Omitted, the floors stay -- which over-quotes rather
    than under-quotes, and only over-quoting is recoverable.

    ``check_availability=False`` prices the service whatever the current load, for a
    caller that remembers the price and checks availability itself on every answer.
    """

    # A node that cannot carry the notice of the service refuses it, as it refuses an
    # architecture that it cannot run (#645).
    notice.enforce_supported(service)
    # The same for the devices of the service (#536): refused, never ignored.
    _enforce_devices(service)

    # Only the launcher checks here (the balancer's local candidate, its preflight), so a
    # refusal for disk alone may free the disk of suspended instances nobody pays for.
    if check_availability and not availability_reclaiming_disk(resources=resources, arch=arch)["can_execute"]:
        return

    # What the start spends: the build, and the builds of the initial children of the
    # service (#538). No balance (#636, 9): the caller adds the balance for the periods
    # that it chooses (`price_bound.funding_sum_mu`), and a start with no `initial_mu`
    # gets the default of the node that runs it. `Configuration.initial_mu` does not
    # change the quote. A free node quotes 0 (#636, 2).
    initial_cost_mu = compute_start_service_cost(metadata=metadata, service=service)

    # Both maintenance figures are quoted for what the instance will *hold*, not for
    # what its manifest asks for. The virtualizer raises anything below a floor when it
    # creates the guest -- undeclared CPU becomes one vCPU, RAM below MIN_MEM_MIB is
    # raised to it, the rootfs image is at least MIN_ROOTFS_BYTES -- and the tick then
    # charges the resolved row, so a quote has to describe that same shape.
    service_hash = _service_hash_or_none(metadata)

    # The price bound of the quote (#636), read once. The maintenance figures are the
    # price at the time of the estimate, at the launch price of now (``P``); the bound is
    # ``P * (1 + v)^k``. A free node quotes the anchor of a launch while free (1x), not 0.
    bound = price_bound.current_bound()

    # The devices that the node would give (#536): a flat rate per second, part of
    # every maintenance period, so the funding of the instance covers them and the
    # father holds the ticks to a figure that includes them.
    from src.devices import pricing as device_pricing

    device_period_mu = device_pricing.charge_mu(
        device_pricing.quote_rate(service), MANAGER_ITERATION_TIME)

    # Calculate estimated cost for local execution.
    return celaut.EstimatedCost(

        # Initial cost: the build and the builds of the initial children. No balance.
        cost=to_amount(initial_cost_mu),

        # Maintenance cost per manager iteration, at the resources it starts with.
        init_maintenance_cost=to_amount(launch_charge_mu(
            system_resources=resolve_billable_resources(
                resources.at_init, service_hash, service=service
            ),
            seconds=MANAGER_ITERATION_TIME,
            arch=arch,
        ) + device_period_mu) if resources.HasField('at_init') else to_amount(device_period_mu),

        # Maintenance cost per manager iteration, at the most it may grow to, at the same
        # price. Not a limit: the limit is this figure times (1 + v)^k when the quote is
        # bounded, and the ticks hold to it (`execution_cost.maintenance_charge_mu`).
        max_maintenance_cost=to_amount(launch_charge_mu(
            system_resources=resolve_billable_resources(
                resources.at_most, service_hash, service=service
            ),
            seconds=MANAGER_ITERATION_TIME,
            arch=arch,
        ) + device_period_mu) if resources.HasField('at_most') else to_amount(device_period_mu),

        # Maintenance frecuency in seconds.
        maintenance_seconds_loop=MANAGER_ITERATION_TIME,

        # price_bounded, price_volatility and volatility_period_seconds.
        **price_bound.to_fields(bound),

        # What the node does at closing time, from its own configuration and never from
        # its load (#344): the next close as an absolute time, 0 if none is scheduled.
        closes_at=activity_window.next_close(),
        on_close=on_close.to_enum(activity_window.effective_on_close()),
    )
