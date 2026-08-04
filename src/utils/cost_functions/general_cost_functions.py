from typing import Dict

from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils.config import ConfigManager
from src.utils.cost_functions.execution_cost import execution_cost, maintain_execution_cost
from src.utils.logger import LOGGER as logger

env_manager = ConfigManager()

# Keys of the rate map this node advertises to peers. Names are part of the wire
# contract: a peer reads them out of Service.Api.Slot.gas_amount_per_call, so
# renaming one silently drops it for everybody who already knows the old name.
RATE_MAINTENANCE_PER_SECOND = "maintenance_max_per_second"
RATE_TUNNEL_OPEN = "tunnel_open"
RATE_TUNNEL_PER_KB = "tunnel_per_kb"


def compute_start_service_cost(
        metadata: celaut.Metadata,
        initial_gas_amount: int,
        resource: celaut_pb2.Service.Container.Resources
) -> int:
    """
    Computes the total initial cost to start a service instance.
    
    Combines execution costs (scaled by gas factor), initial gas allocation, 
    and minimum system resource maintenance costs to determine total startup cost.
    
    Args:
        metadata: Service configuration metadata
        initial_gas_amount: Base gas amount required for service initialization
        resource: Resources specifying minimum system requirements
        
    Returns:
        Total startup cost as an integer value
    """
    return int(sum([
        execution_cost(
            metadata=metadata,
            system_resources=resource.at_init
        ),
        initial_gas_amount,
    ]))


def compute_maintenance_cost(system_resources: celaut.Sysresources) -> int:
    """
    Calculates the ongoing maintenance cost.
    
    Args:
        system_resources: System resources configuration object containing memory limits
        
    Returns:
        Maintenance cost calculated as memory limit multiplied by cost factor
    """
    return maintain_execution_cost(system_resources=system_resources)


def normalized_maintain_cost(cost, timelapse) -> int:
    """
    Adjusts maintenance cost for a specific time period.

    Args:
        cost: Base maintenance cost per time unit
        timelapse: Duration factor to scale the cost

    Returns:
        Time-scaled maintenance cost as integer
    """
    return cost * timelapse


def _rate(key: str, default: float = 0.0) -> float:
    try:
        value = float(env_manager.get(key, default))
        return value if value > 0 else 0.0
    except (TypeError, ValueError):
        logger(f"[RATES] {key} is not a number; advertising it as unset.")
        return 0.0


def node_advertised_rates() -> Dict[str, int]:
    """The node's recurring rates, for peers to read before negotiating anything.

    These are the charges a peer cannot discover any other way. The cost of a
    *specific service* is not here on purpose — that comes from
    ``GetServiceEstimatedCost``, which prices the actual resources requested.

    Every value is a **ceiling**, not a quote:

    * ``maintenance_max_per_second`` — the manager charges
      ``maintain_execution_cost`` once per ``MANAGER_ITERATION_TIME``
      (``src/manager/maintain.py``), and that function is bounded by
      ``EXECUTION_COST`` when supply is exhausted. Dividing by the cadence gives a
      per-second figure that is comparable between nodes whose manager ticks at
      different rates. What a peer actually pays is lower whenever resources are
      available, and there is deliberately no per-GiB or per-vCPU figure: the cost
      model weights resources against current supply, so a fixed price per
      resource does not exist to be advertised.
    * ``tunnel_open`` / ``tunnel_per_kb`` — exact, since tunnel metering is linear
      and independent of load (``src/tunneling/rpc_tunnel.py``).

    A rate of zero is omitted rather than advertised as 0, so "free" is never
    claimed by accident (an unset or malformed key reads as zero too).
    """
    execution_ceiling = _rate("EXECUTION_COST")
    iteration_seconds = _rate("MANAGER_ITERATION_TIME")

    rates = {
        RATE_TUNNEL_OPEN: _rate("costs.TUNNEL_OPEN_COST"),
        RATE_TUNNEL_PER_KB: _rate("costs.TUNNEL_COST_PER_KB"),
    }

    if execution_ceiling and iteration_seconds:
        rates[RATE_MAINTENANCE_PER_SECOND] = execution_ceiling / iteration_seconds

    return {key: int(value) for key, value in rates.items() if int(value) > 0}
