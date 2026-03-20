
import subprocess

from src.utils.config import DOCKER_COMMAND, DOCKER_ENV


def remove(vmachine_id: str) -> bool:
    """Remove a service."""

    try:
        subprocess.run(
            DOCKER_COMMAND + ["rmi", f"{vmachine_id}.docker", "--force"],
            env=DOCKER_ENV,
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"Error executing docker rmi: {e}")
        raise

    return True
