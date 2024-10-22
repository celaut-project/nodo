from protos import celaut_pb2 as celaut, gateway_pb2
from src.builder import build
from src.utils import logger as l
from src.utils.env import DOCKER_CLIENT, EnvManager
from src.utils.verify import get_service_hex_main_hash

env_manager = EnvManager()

MEMORY_LIMIT_COST_FACTOR = env_manager.get_env("MEMORY_LIMIT_COST_FACTOR")
COST_OF_BUILD = env_manager.get_env("COST_OF_BUILD")
COMPUTE_POWER_RATE = env_manager.get_env("COMPUTE_POWER_RATE")
EXECUTION_BENEFIT = env_manager.get_env("EXECUTION_BENEFIT")
GAS_COST_FACTOR = env_manager.get_env("GAS_COST_FACTOR")


def __is_service_built(service_hash: str) -> bool:
    """Check if the service is built by comparing the service hash with existing Docker images."""
    try:
        # Get the list of images
        images = DOCKER_CLIENT().images.list()
        
        # Check if images exist and process tags safely
        for img in images:
            try:
                if img.tags and isinstance(img.tags[0], str):  # Validate tag structure
                    # Extract the hash from the tag and check if it matches the service_hash
                    if service_hash == img.tags[0].split('.')[0]:
                        return True
            except:
                continue
    except (IndexError, AttributeError) as e:
        # Log the error, handle exceptions for missing attributes or invalid indexing
        print(f"An error occurred while checking if service is built: {e}")
    return False


def __build_cost(metadata: celaut.Any.Metadata) -> int:
    """Calculate the cost of building a service based on its metadata and Docker status."""
    try:
        # Get the service hash from the metadata
        service_hash = get_service_hex_main_hash(metadata=metadata)
        
        # Check if the service is already built
        is_built = __is_service_built(service_hash)

        # Check if the architecture is supported
        if not is_built and not build.check_supported_architecture(metadata=metadata):
            raise build.UnsupportedArchitectureException(arch=str(metadata))

        # Calculate the total build cost
        return sum([
            COST_OF_BUILD * (not is_built),  # Add build cost if not already built
            # Add any additional costs here (e.g., cost of obtaining the container) # TODO
        ])

    except Exception as e:
        l.LOGGER('Manager - build cost exception: ' + str(e))
        pass  # Optionally, return a default cost or re-raise the exception

    return COST_OF_BUILD  # Default to return base build cost


def __execution_cost(metadata: celaut.Any.Metadata) -> int:
    l.LOGGER('Get execution cost')
    try:
        return sum([
            len(DOCKER_CLIENT().containers.list()) * COMPUTE_POWER_RATE,
            __build_cost(metadata=metadata),
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
        __execution_cost(
            metadata=metadata
        ) * GAS_COST_FACTOR,
        initial_gas_amount,
        compute_maintenance_cost(system_resources=resource.min_sysreq)
    ]))


def compute_maintenance_cost(system_resources: celaut.Sysresources) -> int:
    return int(MEMORY_LIMIT_COST_FACTOR * system_resources.mem_limit)


def normalized_maintain_cost(cost, timelapse) -> int:
    return cost * timelapse
