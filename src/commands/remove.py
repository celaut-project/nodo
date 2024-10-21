import os

from src.utils.env import EnvManager, DOCKER_COMMAND

env_manager = EnvManager()

METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")
REGISTRY = env_manager.get_env("REGISTRY")
DEFAULT_INTIAL_GAS_AMOUNT = env_manager.get_env("DEFAULT_INTIAL_GAS_AMOUNT")




def remove(service: str):


    # Iterate through the commands and execute each.
    for cmd in [
        f"{DOCKER_COMMAND} rmi {service}.docker --force",
        f"rm -rf {REGISTRY}/{service}",
        f"rm -rf {METADATA_REGISTRY}/{service}"
    ]:
        ret_code = os.system(cmd)
        if ret_code != 0:  # If the return code is not zero, log the failure.
            print(f"Error executing command: {cmd} with return code {ret_code}")
            raise Exception(f"Command failed: {cmd}")

    
    print(f'Service {service} removed from the node.')
