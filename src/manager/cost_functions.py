from typing import Optional, Callable

from protos import celaut_pb2 as celaut, gateway_pb2
from src.balancers.resource_balancer.resource_balancer import ClauseResource, resource_configuration_balancer
from src.builder import build
from src.utils import logger as l
from src.utils.env import MEMORY_LIMIT_COST_FACTOR, DOCKER_CLIENT, COST_OF_BUILD, COMPUTE_POWER_RATE, EXECUTION_BENEFIT, \
    GAS_COST_FACTOR, MANAGER_ITERATION_TIME
from src.utils.utils import to_gas_amount
from src.utils.verify import get_service_hex_main_hash


def maintain_cost(sysreq: dict) -> int:
    return MEMORY_LIMIT_COST_FACTOR * sysreq['mem_limit']


def build_cost(metadata: celaut.Any.Metadata) -> int:
    is_built = (get_service_hex_main_hash(metadata=metadata)
                in [img.tags[0].split('.')[0] for img in DOCKER_CLIENT().images.list()])
    if not is_built and not build.check_supported_architecture(metadata=metadata):
        raise build.UnsupportedArchitectureException(arch=str(metadata))
    try:
        # Coste de construcción si no se posee el contenedor del servicio.
        # Debe de tener en cuenta el coste de buscar el conedor por la red.
        return sum([
            COST_OF_BUILD * (is_built is False),
            # Coste de obtener el contenedor ... #TODO
        ])
    except Exception as e:
        l.LOGGER('Manager - build cost exception: ' + str(e))
        pass
    return COST_OF_BUILD


def execution_cost(metadata: celaut.Any.Metadata) -> int:
    l.LOGGER('Get execution cost')
    try:
        return sum([
            len(DOCKER_CLIENT().containers.list()) * COMPUTE_POWER_RATE,
            build_cost(metadata=metadata),
            EXECUTION_BENEFIT
        ])
    except build.UnsupportedArchitectureException as e:
        raise e
    except Exception as e:
        l.LOGGER('Error calculating execution cost ' + str(e))
        raise e


def compute_start_service_cost(
        metadata: celaut.Any.Metadata,
        initial_gas_amount: int,
        resource: ClauseResource
) -> int:
    return sum([
        execution_cost(
            metadata=metadata
        ) * GAS_COST_FACTOR,
        initial_gas_amount,
        compute_maintenance_cost(resource=resource)
    ])


def compute_maintenance_cost(
        resource: ClauseResource
) -> int:
    return 0  # TODO compute maintenance cost using resources.


def generate_estimated_cost(
        metadata: celaut.Any.Metadata,
        initial_gas_amount: int,
        config: Optional[gateway_pb2.Configuration],
        log: Optional[Callable]
) -> gateway_pb2.EstimatedCost:
    selected_clause: int = resource_configuration_balancer(clauses=dict(config.resources.clause))

    cost: int = compute_start_service_cost(metadata=metadata, initial_gas_amount=initial_gas_amount)
    maintenance_cost: int = compute_maintenance_cost(
        resource=config.resources.clause[selected_clause]
    )

    if log:
        log()

    return gateway_pb2.EstimatedCost(
        cost=to_gas_amount(cost),
        maintenance_cost=to_gas_amount(maintenance_cost),
        maintance_seconds_loop=MANAGER_ITERATION_TIME,
        variance=0,  # TODO dynamic variance.
        comb_resource_selected=selected_clause
    )
