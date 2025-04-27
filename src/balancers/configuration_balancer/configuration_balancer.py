from typing import Dict, Tuple

from protos import celaut_pb2 as celaut
from protos import gateway_pb2
from src.balancers.estimated_cost_sorter.estimated_cost_sorter import estimated_cost_sorter
from src.manager.manager import could_ve_this_sysreq
from src.utils.cost_functions.general_cost_functions import compute_start_service_cost
from src.utils.cost_functions.execution_cost import compute_maintenance_cost
from src.utils.utils import to_gas_amount
from src.utils.env import EnvManager

env_manager = EnvManager()
MANAGER_ITERATION_TIME = env_manager.get_env("MANAGER_ITERATION_TIME")

ClauseResource = gateway_pb2.CombinationResources.Clause
#  TODO make from protos.gateway_pb2.CombinationResource.Clause as ClauseResource


def configuration_balancer(
        clauses: Dict[int, ClauseResource],
        metadata: celaut.Metadata,
        initial_gas_amount: int
) -> Tuple[str, gateway_pb2.EstimatedCost]:
    posible_clauses: Dict[str, gateway_pb2.EstimatedCost] = {}

    # TODO PARTE DE LOS CALCULOS INTERNOS DEL COMPUTO DE COSTES SON LOS MISMOS (EL COSTE DE CONSTRUCCIÓN DEL SERVICIO, ETC ...)
    for _i, clause in clauses.items():
        if not could_ve_this_sysreq(clause.max_sysreq):
            continue

        # Calculate estimated cost for local execution.
        posible_clauses['local'] = gateway_pb2.EstimatedCost(
            
            # Initial cost.
            cost=to_gas_amount(compute_start_service_cost(
                metadata=metadata,
                initial_gas_amount=initial_gas_amount,
                resource=clause
            )),
            
            # Minimal maintenance cost.
            min_maintenance_cost=to_gas_amount(compute_maintenance_cost(
                system_resources=clause.min_sysreq
            )) if clause.HasField('min_sysreq') else to_gas_amount(gas_amount=0),
            
            # Maximum maintenance cost.
            max_maintenance_cost=to_gas_amount(compute_maintenance_cost(
                system_resources=clause.max_sysreq
            )) if clause.HasField('max_sysreq') else to_gas_amount(gas_amount=0),
            
            # Maintenance frecuency in seconds.
            maintenance_seconds_loop=MANAGER_ITERATION_TIME,
            
            # Variance of the three costs.
            variance=0,  # TODO compute_variance
            
            # Index.
            comb_resource_selected=_i
        )

    if len(posible_clauses) == 0:
        raise Exception("Any configuration is supported.")

    return next(estimated_cost_sorter(
        estimated_costs=posible_clauses,
        weight_clauses={_id: clause.cost_weight for _id, clause in clauses.items()}
    ))  # Take the best.
