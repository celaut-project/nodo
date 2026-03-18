import os
import shutil
import subprocess
from src.commands.__by_tag import get_id
from src.utils.config import ConfigManager, DOCKER_COMMAND, DOCKER_ENV
from src.utils.logger import LOGGER as l

env_manager = ConfigManager()

METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")
DEFAULT_INITIAL_GAS_AMOUNT = env_manager.get("DEFAULT_INITIAL_GAS_AMOUNT")


# TODO This command must be generalized into virtualizers/interface.  But first DB must contain the virtualizer used by the vmachine.

def remove(service: str):
    service = get_id(service)

    # Check if script is run as root
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return

    try:
        subprocess.run(
            DOCKER_COMMAND + ["rmi", f"{service}.docker", "--force"],
            env=DOCKER_ENV,
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"Error executing docker rmi: {e}")
        raise

    shutil.rmtree(os.path.join(REGISTRY, service), ignore_errors=True)
    shutil.rmtree(os.path.join(METADATA_REGISTRY, service), ignore_errors=True)

    
    print(f'Service {service} removed from the node.')
