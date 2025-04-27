import psutil
from protos import celaut_pb2 as celaut
from src.utils.utils import read_service_from_disk
from src.virtualizers.docker import build
from src.virtualizers.docker.architecture import check_supported_architecture, UnsupportedArchitectureException
from src.utils.logger import LOGGER as log
from src.utils.env import DOCKER_CLIENT, EnvManager
from src.utils.verify import get_service_hex_main_hash

env_manager = EnvManager()

EXECUTION_COST = env_manager.get_env("EXECUTION_COST")
BUILD_COST = env_manager.get_env("BUILD_COST")
EXECUTION_BENEFIT = env_manager.get_env("EXECUTION_BENEFIT")

# --- Constants ---
# Resource type identifiers
CPU = 'cpu'
MEM = 'mem'
DISK = 'disk'
# List of resources considered by default when instance specifies no limits
DEFAULT_RESOURCES = [CPU, MEM, DISK]
NUM_DEFAULT_RESOURCES = len(DEFAULT_RESOURCES)

# Default weight for each resource if no specific requirements are given by the instance
# Ensures equal weighting (1/3 for CPU, 1/3 for Mem, 1/3 for Disk)
DEFAULT_WEIGHT = 1.0 / NUM_DEFAULT_RESOURCES
DEFAULT_WEIGHTS = {res: DEFAULT_WEIGHT for res in DEFAULT_RESOURCES}

# Factor for exponential scaling of perceived load in cost calculation.
# Determines how sharply the cost increases as resource availability drops.
# A value > 1.0 increases the curve's steepness near scarcity.
# Example: load_factor = (1.0 - supply) ** (1.0 / EXPONENTIAL_COST_FACTOR)
# If factor=4, supply=0.5 -> load=0.84; supply=0.1 -> load=0.97. Higher values mean
# the load factor stays low until supply gets very scarce, then rises sharply.
# Lower values (closer to 1) approach linear scaling.
EXPONENTIAL_COST_FACTOR = 1.0 # Needs tuning based on desired economic behavior

# --- Functions ---

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
        log(f"An error occurred while checking if service is built: {e}")
    return False


def __build_cost(metadata: celaut.Metadata) -> int:
    """Calculate the cost of building a service based on its metadata and Docker status."""
    try:
        # Get the service hash from the metadata
        service_hash = get_service_hex_main_hash(metadata=metadata)
        
        # Check if the service is already built
        if __is_service_built(service_hash):
            return 0
        
        log(f"System has no built container to run service {service_hash}.")

        # Check if the architecture is supported
        if not check_supported_architecture(
            service=read_service_from_disk(service_hash=service_hash), 
            metadata=metadata
        ):
            raise UnsupportedArchitectureException(arch=str(metadata))

        # Calculate the total build cost
        return sum([
            BUILD_COST,
            # Add any additional costs here (e.g., cost of obtaining the container) # TODO
        ])

    except Exception as e:
        log('Manager - build cost exception: ' + str(e))
        raise e

def __get_available_supply(system_resources: celaut.Sysresources) -> float:
    """
    Calculates a weighted resource availability score for a specific service instance.

    This function assesses how suitable the current system resources are for launching
    a given service instance by comparing the instance's requirements (CPU, RAM, Disk limits)
    against the system's total capacity and current availability.

    The core idea is to weight the availability of each resource (CPU, RAM, Disk)
    based on how much that resource is demanded by the *specific instance* relative
    to its other demands. This provides a more nuanced score than just looking at
    raw percentage availability. For example, if an instance needs a lot of CPU but little RAM,
    CPU availability will have a much larger impact on the final score than RAM availability.

    The calculation involves:
    1. Getting the instance's resource limits (requirements).
    2. Getting the system's total capacity for each resource.
    3. Calculating 'demand ratios': (instance_limit / system_total) for each requested resource.
       This represents the fraction of total system capacity the instance requires.
    4. Calculating 'weights': Normalizing the demand ratios so they sum to 1.0.
       The weight of a resource reflects its relative importance *to this instance*.
       If no limits are specified, equal default weights (1/3 each) are used, defined
       by DEFAULT_WEIGHTS based on DEFAULT_RESOURCES.
    5. Getting the *current* percentage availability of each resource on the system.
    6. Calculating the weighted sum: Sum(current_availability_percent[res] * weight[res]).
    7. Normalizing the final score to be between 0.0 (completely unsuitable / no resources)
       and 1.0 (ideal availability considering the instance's needs).

    Args:
        system_resources (celaut.Sysresources): An object containing the service instance's
                          resource requirements (e.g., cpu_limit, mem_limit, disk_limit).

    Returns:
        float: A normalized availability score between 0.0 and 1.0.
               Returns 0.0 if a calculation error occurs or if essential system
               information cannot be retrieved. Note: 0.0 might indicate either
               truly zero weighted availability or a calculation failure, which
               could lead to maximum cost in the execution_cost function.
    """
    try:
        # 1. Get service requirements (limits) from the input object
        service_cpu = getattr(system_resources, 'cpu_limit', None)
        service_mem = getattr(system_resources, 'mem_limit', None)
        service_disk = getattr(system_resources, 'disk_limit', None)

        # 2. Get system's total capacity
        # Use physical cores as the measure for CPU capacity. Ensure this aligns
        # with the unit used in service_cpu (e.g., if service_cpu is milliCPUs, convert).
        system_cpu = psutil.cpu_count(logical=False) or 0
        system_mem = psutil.virtual_memory().total
        # Assumes '/' is the relevant disk partition. Make configurable if needed.
        system_disk = psutil.disk_usage('/').total

        # Check for zero capacity to avoid division by zero errors later
        if not system_cpu or not system_mem or not system_disk:
            log(f"[WARNING] System reported zero capacity for CPU ({system_cpu}), "
                        f"Memory ({system_mem}) or Disk ({system_disk}). Cannot calculate supply.")
            return 0.0

        # 3. Calculate demand ratios: (instance_requirement / system_total)
        #    Only include ratios for resources explicitly requested *and positive*.
        demand_ratios = {}
        if service_cpu is not None and service_cpu > 0:
            demand_ratios[CPU] = service_cpu / system_cpu
        if service_mem is not None and service_mem > 0:
            demand_ratios[MEM] = service_mem / system_mem
        if service_disk is not None and service_disk > 0:
            demand_ratios[DISK] = service_disk / system_disk

        # 4. Determine resource weights based on relative demand
        resources_with_demand = list(demand_ratios.keys())
        if not resources_with_demand:
            # No specific positive demands provided. Fall back to equal default weights.
            log("No positive resource limits specified by instance. Using default equal weights.")
            weights = DEFAULT_WEIGHTS
            # Check availability for all default resources in this case.
            resources_to_check = DEFAULT_RESOURCES
        else:
            # Calculate weights based on the instance's specific demand profile.
            total_demand_ratio = sum(demand_ratios.values())

            # Check if total_demand_ratio is positive to avoid division by zero or negative weights
            if total_demand_ratio <= 0:
                 log(f"[WARNING] Total demand ratio is zero or negative ({total_demand_ratio}). "
                             "Falling back to default weights.")
                 weights = DEFAULT_WEIGHTS
                 resources_to_check = DEFAULT_RESOURCES
            else:
                 # Weight for each resource = its demand ratio / sum of all demand ratios.
                 # This ensures weights sum to 1.0 and reflect relative importance.
                 weights = {res: demand_ratios[res] / total_demand_ratio for res in resources_with_demand}
                 resources_to_check = resources_with_demand
                 # Ensure weights dictionary covers all default resources, setting weight to 0
                 # for those not explicitly demanded, for consistent calculations later.
                 for res in DEFAULT_RESOURCES:
                     weights.setdefault(res, 0.0)

        # 5. Get current system availability percentages for relevant resources
        current_availability_percent = {}
        # Only query psutil for resources that have a non-zero weight
        if CPU in resources_to_check and weights.get(CPU, 0) > 0:
            # cpu_percent gives usage; availability is 100 - usage.
            # interval=0.1 provides a quick snapshot; adjust if needed.
            cpu_usage = psutil.cpu_percent(interval=0.1)
            current_availability_percent[CPU] = max(0.0, 100.0 - cpu_usage)
        if MEM in resources_to_check and weights.get(MEM, 0) > 0:
            mem_info = psutil.virtual_memory()
            # Use mem_info.available for a more realistic measure of free RAM
            current_availability_percent[MEM] = (mem_info.available / system_mem) * 100.0 if system_mem else 0.0
        if DISK in resources_to_check and weights.get(DISK, 0) > 0:
            # Assuming '/' is the relevant disk partition.
            disk_info = psutil.disk_usage('/')
            current_availability_percent[DISK] = (disk_info.free / system_disk) * 100.0 if system_disk else 0.0

        # 6. Calculate the weighted sum of available resources
        #    Iterate through the weights dict to ensure all relevant resources are considered.
        weighted_sum = 0.0
        for res, weight in weights.items():
             if weight > 0: # Only factor in resources with positive weight
                 # Get the measured availability, default to 0.0 if not measured (should not happen with checks above)
                 availability = current_availability_percent.get(res, 0.0)
                 weighted_sum += availability * weight

        # 7. Normalize the weighted sum (which is 0-100) to a score between 0.0 and 1.0
        normalized_score = max(0.0, min(weighted_sum / 100.0, 1.0))

        log(f"Calculated availability score: {normalized_score:.4f} "
                  f"(Weights: { {k: f'{v:.2f}' for k, v in weights.items()} }, " # Format weights for readability
                  f"Availability %: { {k: f'{v:.1f}' for k, v in current_availability_percent.items()} })") # Format availability
        return normalized_score

    except psutil.Error as pe:
        log(f"[ERROR] psutil error during resource supply calculation: {pe}")
        return 0.0 # Return 0.0 on error as per original logic
    except Exception as e:
        # Log the full traceback for unexpected errors
        log(f"[ERROR] General error during resource supply calculation: {e}")
        return 0.0 # Return 0.0 on error as per original logic

def maintain_execution_cost(system_resources: celaut.Sysresources) -> int:
    # Get the weighted available supply score (0.0 to 1.0)
    available_supply = __get_available_supply(system_resources=system_resources)

    # Calculate the 'lack of supply' (ranges from 0.0 when supply=1.0, to 1.0 when supply=0.0)
    lack_of_supply = 1.0 - available_supply

    # Calculate the effective load factor using exponential scaling.
    # This factor approaches 1.0 much faster than the linear 'lack_of_supply'
    # as available_supply drops towards 0, thus increasing cost sharply under scarcity.
    if EXPONENTIAL_COST_FACTOR <= 0:
            log("EXPONENTIAL_COST_FACTOR must be positive.")
            # Defaulting to linear factor to avoid math error, but this indicates misconfiguration.
            used_compute_power_factor = lack_of_supply
    elif lack_of_supply >= 1.0:
            # If supply is effectively zero, the factor is maximum (1.0)
            used_compute_power_factor = 1.0
    elif lack_of_supply <= 0.0:
            # If supply is 1.0 or more, factor is minimum (0.0)
            used_compute_power_factor = 0.0
    else:
            # Apply the exponential scaling: factor = lack**(1/exp_factor)
            # This maps the curve so it rises steeply near lack=1 (supply=0)
            used_compute_power_factor = lack_of_supply ** (1.0 / EXPONENTIAL_COST_FACTOR)

    log(f"Supply Score: {available_supply:.4f}, Lack of Supply: {lack_of_supply:.4f}, "
                f"Exponential Load Factor: {used_compute_power_factor:.4f}")
    
    cost = used_compute_power_factor * EXECUTION_COST
    return int(round(cost))

def execution_cost(metadata: celaut.Metadata, system_resources: celaut.Sysresources) -> int:
    """
    Calculates the estimated execution cost for running a service instance.

    The cost is composed of three main parts:
    1. Compute Power Cost: Based on the perceived system load relative to the
       instance's needs. This uses the `__get_available_supply` score and applies
       an exponential factor (`EXPONENTIAL_COST_FACTOR`) to heavily penalize
       running on systems where relevant resources are scarce.
    2. Build Cost: The cost associated with building the service container,
       obtained via `__build_cost` using service metadata.
    3. Execution Benefit/Offset: A fixed value (`EXECUTION_BENEFIT`) representing
       a baseline operational cost or incentive.

    Args:
        metadata (celaut.Metadata): Service metadata for build cost calculation.
        system_resources (celaut.Sysresources): Service's resource requirements for supply calculation.

    Returns:
        int: The total estimated execution cost, rounded to the nearest integer.

    Raises:
        build.UnsupportedArchitectureException: If build cost calculation fails due to architecture.
        Exception: Propagates errors from supply calculation or other unexpected issues.
    """
    log('Calculating execution cost...')
    try:
        # Calculate the individual cost components
        compute_cost = maintain_execution_cost(system_resources=system_resources)
        build_c = __build_cost(metadata=metadata)
        benefit = EXECUTION_BENEFIT

        # Calculate total cost
        total_cost = compute_cost + build_c + benefit

        log(f"Execution cost calculated: {int(round(total_cost))} "
                 f"(Compute: {compute_cost:e}, Build: {build_c:e}, Benefit: {benefit:e})")

        return int(round(total_cost))

    except build.UnsupportedArchitectureException as e:
        log(f"[ERROR] Build error due to unsupported architecture: {e}")
        raise e # Propagate specific build error
    except Exception as e:
        log(f"[ERROR] General error calculating execution cost: {e}")
        raise e # Propagate other errors
