import os
from pathlib import Path
import docker as docker_lib
from src.gateway.launcher.local_execution.set_config import get_config, write_config
from src.manager.manager import add_container, get_dev_clients
from src.utils.env import EnvManager

# Crear cliente Docker
client = docker_lib.from_env()
env_manager = EnvManager()
DEFAULT_INITIAL_GAS_AMOUNT = env_manager.get_env("DEFAULT_INITIAL_GAS_AMOUNT")


def __cleanup_existing_container(container_name: str) -> None:
    """
    Clean up any existing container and image with the specified name.
    
    Args:
        container_name (str): Name of the container/image to clean up
    """
    # Find all containers and images with the specified name
    containers = client.containers.list(all=True, filters={"ancestor": container_name})
    for container in containers:
        print(f"Stopping and removing container '{container.name}'...")
        container.stop()
        container.remove()

    images = client.images.list(name=container_name)
    for image in images:
        print(f"Removing Docker image '{image.tags[0]}'...")
        client.images.remove(image.id, force=True)


def __build_container_image(service_dir: Path, container_name: str) -> None:
    """
    Build the Docker container image.
    
    Args:
        service_dir (Path): Path to the service directory
        container_name (str): Name for the container image
    """
    print(f"Building Docker image '{container_name}' from directory '{service_dir}'...")
    client.images.build(path=str(service_dir), tag=container_name)


def __run_container(image_id: str, port: str) -> docker_lib.models.containers.Container:
    """
    Run a Docker container.
    
    Args:
        image_id (str): ID of the image to run
        port (str): Port to expose
    """
    print(f"Running Docker container '{image_id}' on port {port}...")
    return client.containers.run(
        image=image_id,
        ports={f"{port}/tcp": int(port)},
        detach=True,
        tty=True,
        stdin_open=True
    )


def __interactive_dev_container(service_path: str) -> str:
    """
    Prepare and run a container for development.

    Args:
        service_path (str): Path to the service directory
    """
    config_path = Path(service_path) / "__config__"
    if not config_path.exists():
        print("Creating config file for development...")
        config = get_config(config=None, resources=None)
        write_config(path=service_path, config=config)

    image_id = f"{Path(service_path).resolve().name}-container"
    __cleanup_existing_container(container_name=image_id)
    __build_container_image(service_dir=service_path, container_name=image_id)
    
    # Create internal service
    port = "5000"  # Should take from .service/service.json api [0] port...
    container = __run_container(image_id=image_id, port=port)
    
    add_container(
        father_id=next(get_dev_clients(gas_amount=DEFAULT_INITIAL_GAS_AMOUNT)),
        container=container,
        initial_gas_amount=None,
        system_requirements_range=None
    )


def run_dev(path: str):
    # Check if script is run as root
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return

    print(f"Executing container for development purposes, project: {path}")
    __interactive_dev_container(service_path=path)
