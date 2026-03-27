from typing import Callable
import docker as docker_lib
from src.utils.runtime import DOCKER_CLIENT

def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    try:
        container = DOCKER_CLIENT().containers.get(vmachine_id)
        if debug_mode: log.LOGGER(f"Container {vmachine_id} status: {container.status}")
        if container.status == 'exited':
            log.LOGGER(f"Instance {vmachine_id} has exited. Removing and penalizing.")
            remove_and_penalize(vmachine_id=vmachine_id)
    except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
        log.LOGGER(f"Error fetching container {vmachine_id}: {str(e)}. Assuming it does not exist.")
        remove_and_penalize(vmachine_id=vmachine_id)
