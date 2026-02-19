import docker as docker_lib

from src.utils import logger as log
from src.utils.config import DOCKER_CLIENT

def stop_container(container_id: str) -> None:
    # TODO Maybe the container not exists.
    """
    Stops a running Docker container.

    Args:
        container_id (str): ID or name of the container to stop.
    """
    client = DOCKER_CLIENT()
    try:
        container = client.containers.get(container_id)
        container.stop() # remove(force=True) <-- TODO on stable.
        log.LOGGER(f"Container '{container_id}' stopped successfully.")
    except docker_lib.errors.NotFound as e:
        log.LOGGER(f"CONTAINER NOT FOUND: {container_id}")
        raise e
    except docker_lib.errors.APIError as e:
        log.LOGGER(f"DOCKER API ERROR WHILE STOPPING '{container_id}' -> {e.explanation}")
        raise e
    except Exception as e:
        log.LOGGER(f"UNEXPECTED ERROR WHILE STOPPING '{container_id}' -> {str(e)}")
        raise e
