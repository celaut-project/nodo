from protos import celaut_pb2 as celaut, gateway_pb2
from src.utils.cost_functions.execution_cost import execution_cost
from src.utils.env import EnvManager

env_manager = EnvManager()

MEMORY_LIMIT_COST_FACTOR = env_manager.get_env("MEMORY_LIMIT_COST_FACTOR")
GAS_COST_FACTOR = env_manager.get_env("GAS_COST_FACTOR")


def compute_start_service_cost(
        metadata: celaut.Metadata,
        initial_gas_amount: int,
        resource: gateway_pb2.CombinationResources.Clause
) -> int:
    """
    Computes the total initial cost to start a service instance.
    
    Combines execution costs (scaled by gas factor), initial gas allocation, 
    and minimum system resource maintenance costs to determine total startup cost.
    
    Args:
        metadata: Service configuration metadata
        initial_gas_amount: Base gas amount required for service initialization
        resource: Resource clause specifying minimum system requirements
        
    Returns:
        Total startup cost as an integer value
    """
    return int(sum([
        execution_cost(
            metadata=metadata,
            system_resources=resource.min_sysreq
        ) * GAS_COST_FACTOR,
        initial_gas_amount,
        compute_maintenance_cost(system_resources=resource.min_sysreq)
    ]))


def compute_maintenance_cost(system_resources: celaut.Sysresources) -> int:
    """
    Calculates the ongoing maintenance cost based on memory allocation.
    
    Args:
        system_resources: System resources configuration object containing memory limits
        
    Returns:
        Maintenance cost calculated as memory limit multiplied by cost factor
    """
    # TODO implement (and update comment) for other parameters.
    return int(MEMORY_LIMIT_COST_FACTOR * system_resources.mem_limit)


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
