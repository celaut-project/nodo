from typing import Optional
from protos import celaut_pb2 as celaut, celaut_pb2
from typing import Dict, Tuple

from protos import celaut_pb2 as celaut
from src.utils.utils import from_gas_amount
from src.balancers.estimated_cost_sorter.estimated_cost_sorter import estimated_cost_sorter
from src.manager.manager import could_ve_this_sysreq
from src.utils.cost_functions.execution_cost import is_free_gas
from src.utils.cost_functions.general_cost_functions import compute_start_service_cost, compute_maintenance_cost
from src.utils.utils import to_gas_amount
from src.utils.env import EnvManager

env_manager = EnvManager()
MANAGER_ITERATION_TIME = env_manager.get_env("MANAGER_ITERATION_TIME")

def generate_estimated_cost(
        metadata: celaut.Metadata,
        config: celaut.Configuration,
        resources: celaut.Service.Container.Resources
) -> Optional[celaut_pb2.EstimatedCost]:

    initial_gas_amount=from_gas_amount(config.initial_gas_amount)

    if resources.at_most and not could_ve_this_sysreq(resources.at_most):
        return

    if is_free_gas(system_resources=resources.at_init):
        initial_gas = 0
    else:
        initial_gas = compute_start_service_cost(
                metadata=metadata,
                initial_gas_amount=initial_gas_amount,
                resource=resources
            )

    # Calculate estimated cost for local execution.
    return celaut.EstimatedCost(
        
        # Initial cost.
        cost=to_gas_amount(initial_gas),
        
        # Minimal maintenance cost.
        min_maintenance_cost=to_gas_amount(compute_maintenance_cost(
            system_resources=resources.at_init
        )) if resources.HasField('at_init') else to_gas_amount(gas_amount=0),
        
        # Maximum maintenance cost.
        max_maintenance_cost=to_gas_amount(compute_maintenance_cost(
            system_resources=resources.at_most
        )) if resources.HasField('at_most') else to_gas_amount(gas_amount=0),
        
        # Maintenance frecuency in seconds.
        maintenance_seconds_loop=MANAGER_ITERATION_TIME,
        
        # Variance of the three costs.
        variance=0,  # TODO compute_variance
    )
