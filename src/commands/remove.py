import os
import shutil
from src.commands.__by_tag import get_id
from src.utils.config import ConfigManager

env_manager = ConfigManager()

METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")
DEFAULT_INITIAL_GAS_AMOUNT = env_manager.get("DEFAULT_INITIAL_GAS_AMOUNT")

def remove(service: str):
    service = get_id(service)

    # Check if script is run as root
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return

    registry_path = os.path.join(REGISTRY, service)
    if os.path.isfile(registry_path):
        os.remove(registry_path)
    elif os.path.isdir(registry_path):
        shutil.rmtree(registry_path)
    else:
        print(f"Warning: {registry_path} does not exist.")

    metadata_path = os.path.join(METADATA_REGISTRY, service)
    if os.path.isfile(metadata_path):
        os.remove(metadata_path)
    elif os.path.isdir(metadata_path):
        shutil.rmtree(metadata_path)
    else:
        print(f"Warning: {metadata_path} does not exist.")
    
    print(f'Service {service} removed from the node.')
