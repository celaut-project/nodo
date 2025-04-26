import psutil
from protos import celaut_pb2 as celaut, gateway_pb2
from src.utils.utils import read_service_from_disk
from src.virtualizers.docker import build
from src.virtualizers.docker.architecture import check_supported_architecture, UnsupportedArchitectureException
from src.utils import logger as log
from src.utils.env import DOCKER_CLIENT, EnvManager
from src.utils.verify import get_service_hex_main_hash

env_manager = EnvManager()

MEMORY_LIMIT_COST_FACTOR = env_manager.get_env("MEMORY_LIMIT_COST_FACTOR")
COST_OF_BUILD = env_manager.get_env("COST_OF_BUILD")
COMPUTE_POWER_RATE = env_manager.get_env("COMPUTE_POWER_RATE")
EXECUTION_BENEFIT = env_manager.get_env("EXECUTION_BENEFIT")
GAS_COST_FACTOR = env_manager.get_env("GAS_COST_FACTOR")


def __is_service_built(service_hash: str) -> bool: 
    # TODO Needs to be on virtualizers/
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
        log.LOGGER(f"An error occurred while checking if service is built: {e}")
    return False


def __build_cost(metadata: celaut.Metadata) -> int:
    """Calculate the cost of building a service based on its metadata and Docker status."""
    try:
        # Get the service hash from the metadata
        service_hash = get_service_hex_main_hash(metadata=metadata)
        
        # Check if the service is already built
        if __is_service_built(service_hash):
            return 0
        
        log.LOGGER(f"System has no built container to run service {service_hash}.")

        # Check if the architecture is supported
        if not check_supported_architecture(
            service=read_service_from_disk(service_hash=service_hash), 
            metadata=metadata
        ):
            raise UnsupportedArchitectureException(arch=str(metadata))

        # Calculate the total build cost
        return sum([
            COST_OF_BUILD,
            # Add any additional costs here (e.g., cost of obtaining the container) # TODO
        ])

    except Exception as e:
        log.LOGGER('Manager - build cost exception: ' + str(e))
        raise e

def __get_available_supply(system_resources: celaut.Sysresources) -> float:
    """
    Calculates available resource supply score weighted by instance requirements.
    
    Dynamically adjusts CPU/RAM/disk weights based on the instance's resource demands
    relative to total system capacity. Returns a normalized score (0.0-1.0) where
    1.0 = ideal for this service, 0.0 = insufficient resources.
    
    Args:
        system_resources: The service's required system resources (cpu/mem/disk)
        
    Returns:
        float: Availability score between 0.0 (no resources) and 1.0 (ideal)
    """
    try:
        # Get service requirements, defaulting to None if missing
        service_cpu  = getattr(system_resources, 'cpu_limit',  None)
        service_mem  = getattr(system_resources, 'mem_limit',  None)
        service_disk = getattr(system_resources, 'disk_limit', None)

        # Get system's total capacity
        system_cpu  = psutil.cpu_count(logical=False) or 0
        system_mem  = psutil.virtual_memory().total
        system_disk = psutil.disk_usage('/').total

        # Build demand_ratios only for the limits that exist
        demand_ratios = {}
        if service_cpu is not None and system_cpu:
            demand_ratios['cpu'] = service_cpu / system_cpu
        if service_mem is not None and system_mem:
            demand_ratios['mem'] = service_mem / system_mem
        if service_disk is not None and system_disk:
            demand_ratios['disk'] = service_disk / system_disk

        # If no demands specified, fall back to equal weights
        resources = list(demand_ratios.keys())
        if not resources:
            weights = {'cpu': 0.35, 'mem': 0.35, 'disk': 0.3}
        else:
            total_demand = sum(demand_ratios.values())
            weights = {res: demand_ratios[res] / total_demand for res in resources}

        # Get current availability percentages
        current = {}
        if 'cpu' in weights:
            current['cpu'] = max(0, 100 - psutil.cpu_percent(interval=0.1))
        if 'mem' in weights:
            current['mem'] = (psutil.virtual_memory().available / system_mem) * 100
        if 'disk' in weights:
            current['disk'] = (psutil.disk_usage('/').free / system_disk) * 100

        # Weighted sum of available resources
        weighted_sum = sum(current[res] * weight for res, weight in weights.items())

        # Normalize to 0–1
        return max(0.0, min(weighted_sum / 100, 1.0))

    except Exception as e:
        log.LOGGER(f"Resource supply calculation error: {e}")
        return 0.0

def __execution_cost(metadata: celaut.Metadata, system_resources: celaut.Sysresources) -> int:
    """
    Calculates the execution cost for running a service instance.
    
    This internal method computes costs by combining container compute power costs, 
    build costs from metadata, and a fixed execution benefit offset. Handles 
    architecture compatibility checks through exception propagation.
    
    Args:
        metadata: Service metadata containing build configuration and requirements.
        
    Returns:
        Total execution cost as an integer value.
        
    Raises:
        build.UnsupportedArchitectureException: If metadata specifies an unsupported architecture.
        Exception: Propagates general calculation errors with logged details.
    """
    log.LOGGER('Get execution cost')
    try:
        used_compute_power = 1 - __get_available_supply(system_resources=system_resources)
        log.LOGGER(f"Current compute power used: {used_compute_power}% (weighted by instance requirements)")
        return sum([
            used_compute_power * COMPUTE_POWER_RATE,
            __build_cost(metadata=metadata),
            EXECUTION_BENEFIT
        ])
    except build.UnsupportedArchitectureException as e:
        raise e
    except Exception as e:
        log.LOGGER('Error calculating execution cost ' + str(e))
        raise e


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
        __execution_cost(
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
