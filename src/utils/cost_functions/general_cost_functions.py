from protos import celaut_pb2 as celaut, gateway_pb2
from src.builder import build
from src.utils import logger as l
from src.utils.env import MEMORY_LIMIT_COST_FACTOR, DOCKER_CLIENT, COST_OF_BUILD, COMPUTE_POWER_RATE, EXECUTION_BENEFIT, \
    GAS_COST_FACTOR
from src.utils.verify import get_service_hex_main_hash


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
        resource: gateway_pb2.CombinationResources.Clause
) -> int:
    return int(sum([
        execution_cost(
            metadata=metadata
        ) * GAS_COST_FACTOR,
        initial_gas_amount,
        compute_maintenance_cost(system_resources=resource.min_sysreq)
    ]))


def compute_maintenance_cost(system_resources: celaut.Sysresources) -> int:
    return int(MEMORY_LIMIT_COST_FACTOR * system_resources.mem_limit)


def normalized_maintain_cost(cost, timelapse) -> int:
    return cost * timelapse
