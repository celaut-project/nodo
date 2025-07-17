import psutil
from protos import celaut_pb2 as celaut
from src.utils.utils import read_service_from_disk
from src.virtualizers.docker import build
from src.virtualizers.docker.architecture import check_supported_architecture, UnsupportedArchitectureException
from src.utils.logger import LOGGER as logger
from src.utils.config import DOCKER_CLIENT, ConfigManager
from src.utils.verify import get_service_hex_main_hash
from src.utils.cost_functions.execution_cost import execution_cost

# Case 1: Instance with specific requirements (CPU heavy)
print("\n--- CASE 1: CPU-Heavy Instance ---")
req1 = celaut.Sysresources(cpu=psutil.cpu_count(logical=False) * 0.5, mem=1024*1024*500, disk=1024*1024*1024*1) # Needs 50% CPU, 500MB RAM, 1GB Disk
meta1 = celaut.Metadata()
try:
    cost1 = execution_cost(meta1, req1)
    print(f"Estimated Cost 1: {cost1}")
except Exception as e:
    print(f"Error calculating cost 1: {e}")

# Case 2: Instance with no requirements (uses default weights)
print("\n--- CASE 2: Instance with No Limits ---")
req2 = celaut.Sysresources()
meta2 = celaut.Metadata()
try:
    cost2 = execution_cost(meta2, req2)
    print(f"Estimated Cost 2: {cost2}")
except Exception as e:
    print(f"Error calculating cost 2: {e}")

# Case 3: Simulate very low available supply (e.g., by manually setting supply)
print("\n--- CASE 3: Simulating Low Supply (Exponential Cost) ---")
# We can't easily force low supply via psutil, so let's patch __get_available_supply temporarily
original_get_supply = __get_available_supply
def mock_low_supply(system_resources): return 0.1 # Simulate 10% weighted supply
__get_available_supply = mock_low_supply
try:
    cost3_low = execution_cost(meta1, req1) # Use req1, doesn't matter for mock
    print(f"Estimated Cost with 10% Supply: {cost3_low}")
except Exception as e:
    print(f"Error calculating cost 3: {e}")
# Restore original function
__get_available_supply = original_get_supply

# Case 4: Simulate almost zero supply
print("\n--- CASE 4: Simulating Near-Zero Supply ---")
def mock_zero_supply(system_resources): return 0.01 # Simulate 1% weighted supply
__get_available_supply = mock_zero_supply
try:
    cost4_zero = execution_cost(meta1, req1)
    print(f"Estimated Cost with 1% Supply: {cost4_zero}")
except Exception as e:
    print(f"Error calculating cost 4: {e}")
__get_available_supply = original_get_supply

# Case 5: Simulate high supply
print("\n--- CASE 5: Simulating High Supply ---")
def mock_high_supply(system_resources): return 0.95 # Simulate 95% weighted supply
__get_available_supply = mock_high_supply
try:
    cost5_high = execution_cost(meta1, req1)
        print(f"Estimated Cost with 95% Supply: {cost5_high}")
    except Exception as e:
        print(f"Error calculating cost 5: {e}")
    __get_available_supply = original_get_supply