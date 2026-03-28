import docker as docker_lib

from src.utils import logger as log
from src.utils.runtime import DOCKER_CLIENT

def kill(vmachine_id: str) -> bool:
    # TODO Maybe the container not exists.
    """
    Stops a running Docker container.

    Args:
        vmachine_id (str): ID or name of the container to stop.
    """
    client = DOCKER_CLIENT()
    try:
        container = client.containers.get(vmachine_id)
        container.stop() # remove(force=True) <-- TODO on stable.
        log.LOGGER(f"Container '{vmachine_id}' stopped successfully.")
        return True
    except docker_lib.errors.NotFound as e:
        log.LOGGER(f"CONTAINER NOT FOUND: {vmachine_id}")
        raise e
    except docker_lib.errors.APIError as e:
        log.LOGGER(f"DOCKER API ERROR WHILE STOPPING '{vmachine_id}' -> {e.explanation}")
        raise e
    except Exception as e:
        log.LOGGER(f"UNEXPECTED ERROR WHILE STOPPING '{vmachine_id}' -> {str(e)}")
        raise e
