from typing import Any, Dict, Optional
import psutil

from protos import celaut_pb2 as celaut, celaut_pb2
from src.manager.resources import IOBigData, could_ve_this_sysreq
from src.utils.cost_functions.general_cost_functions import compute_start_service_cost, compute_maintenance_cost
from src.utils.utils import from_amount, to_amount
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger
from src.utils.verify import get_service_hex_main_hash
from src.virtualizers.interface import resolve_billable_resources

env_manager = ConfigManager()
MANAGER_ITERATION_TIME = env_manager.get("MANAGER_ITERATION_TIME")


def _get_service_memory_snapshot() -> tuple[int, int]:
    # This is the same memory pool used by `could_ve_this_sysreq` through IOBigData.
    try:
        io_big_data = IOBigData()
        pool_total = int(io_big_data.ram_pool()) if callable(io_big_data.ram_pool) else 0
        pool_available = int(io_big_data.get_ram_avaliable()) if callable(io_big_data.get_ram_avaliable) else 0
        return pool_total, pool_available
    except Exception:
        memory = psutil.virtual_memory()
        fallback = int(memory.available)
        return fallback, fallback


def get_resource_availability(resources: celaut.Service.Container.Resources) -> Dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu_total = psutil.cpu_count(logical=False) or 0
    # Non-blocking: interval=None is usage since the previous call. interval=0.1
    # slept 100 ms per invocation for a field nothing reads (#288).
    cpu_available_percent = max(0.0, 100.0 - psutil.cpu_percent(interval=None))

    service_memory_pool_total, service_memory_pool_available = _get_service_memory_snapshot()

    requested_mem_limit = 0
    if resources and resources.HasField("at_most") and resources.at_most.HasField("mem_limit"):
        requested_mem_limit = int(resources.at_most.mem_limit)

    can_execute = True
    reason = ""
    if resources and resources.HasField("at_most") and not could_ve_this_sysreq(resources.at_most):
        can_execute = False
        reason = (
            "Insufficient memory for resources.at_most.mem_limit. "
            f"Requested: {requested_mem_limit} bytes, "
            f"available in service memory pool: {service_memory_pool_available} bytes, "
            f"total service memory pool: {service_memory_pool_total} bytes."
            f"\n Try `sudo nodo daemon restart` to free up memory or increase the memory pool size if possible."
        )

    return {
        "can_execute": can_execute,
        "reason": reason,
        "requested_mem_limit": requested_mem_limit,
        "service_memory_pool_total": service_memory_pool_total,
        "service_memory_pool_available": service_memory_pool_available,
        "system_memory_total": int(memory.total),
        "system_memory_available": int(memory.available),
        "system_disk_total": int(disk.total),
        "system_disk_free": int(disk.free),
        "system_cpu_total": int(cpu_total),
        "system_cpu_available_percent": float(cpu_available_percent),
    }


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
        resources: celaut.Service.Container.Resources
) -> Optional[celaut_pb2.EstimatedCost]:

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
        )) if resources.HasField('at_init') else to_amount(0),

        # Maintenance cost per manager iteration, at the most it may grow to.
        max_maintenance_cost=to_amount(compute_maintenance_cost(
            system_resources=resolve_billable_resources(resources.at_most, service_hash),
            seconds=MANAGER_ITERATION_TIME,
        )) if resources.HasField('at_most') else to_amount(0),
        
        # Maintenance frecuency in seconds.
        maintenance_seconds_loop=MANAGER_ITERATION_TIME,
        
        # Variance of the three costs.
        variance=0,  # TODO compute_variance
    )
