from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils.cost_functions.execution_cost import execution_cost, maintain_execution_cost


def compute_start_service_cost(
        metadata: celaut.Metadata,
        initial_gas_amount: int,
        resource: celaut_pb2.Service.Container.Resource
) -> int:
    """
    Computes the total initial cost to start a service instance.
    
    Combines execution costs (scaled by gas factor), initial gas allocation, 
    and minimum system resource maintenance costs to determine total startup cost.
    
    Args:
        metadata: Service configuration metadata
        initial_gas_amount: Base gas amount required for service initialization
        resource: Resource specifying minimum system requirements
        
    Returns:
        Total startup cost as an integer value
    """
    return int(sum([
        execution_cost(
            metadata=metadata,
            system_resources=resource.min_sysreq
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
