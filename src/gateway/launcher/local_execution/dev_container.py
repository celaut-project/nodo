import json
import subprocess
from pathlib import Path
from typing import Optional
from protos import celaut_pb2 as celaut
from src.gateway.launcher.local_execution.set_config import get_config, set_config, write_config
from src.utils.env import DEFAULT_SYSTEM_RESOURCES

def __prepare_container_environment(service_path: str) -> tuple[Path, str, dict]:
    """
    Prepare and validate the container environment.
    
    Args:
        service_path (str): Path to the service directory
        
    Returns:
        tuple[Path, str, dict]: Service directory path, container name, and pre-compile configuration
        
    Raises:
        FileNotFoundError: If required configuration files are missing
        ValueError: If workdir is not specified or invalid
    """
    service_dir = Path(service_path).resolve()
    config_path = service_dir / '.service' / 'pre-compile.json'
    dockerfile_path = service_dir / '.service' / 'Dockerfile'
    
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    if not dockerfile_path.exists():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile_path}")
    
    with open(config_path, 'r') as file:
        pre_compile = json.load(file)
    
    container_workdir = pre_compile.get('workdir')
    if not container_workdir:
        raise ValueError("Workdir not specified in pre-compile.json")
    
    if service_dir.name != container_workdir:
        raise ValueError("Workdir in pre-compile.json must match repo folder name")
    
    container_name = f"{service_dir.name}-container"
    
    return service_dir, container_name, pre_compile

def __cleanup_existing_container(container_name: str) -> None:
    """
    Clean up any existing container and image with the specified name.
    
    Args:
        container_name (str): Name of the container/image to clean up
    """
    existing_images = subprocess.run(
        ["docker", "images", "-q", container_name],
        capture_output=True, text=True
    )
    
    if existing_images.stdout.strip():
        print(f"Cleaning up existing Docker image '{container_name}'...")
        
        containers = subprocess.run(
            ["docker", "ps", "-a", "-q", "--filter", f"ancestor={container_name}"],
            capture_output=True, text=True
        )
        
        if containers.stdout.strip():
            container_ids = containers.stdout.strip().splitlines()
            for container_id in container_ids:
                subprocess.run(["docker", "stop", container_id], check=True)
                subprocess.run(["docker", "rm", container_id], check=True)
                
        subprocess.run(["docker", "rmi", "-f", container_name], check=True)

def __build_container_image(service_dir: Path, container_name: str) -> None:
    """
    Build the Docker container image.
    
    Args:
        service_dir (Path): Path to the service directory
        container_name (str): Name for the container image
    """
    dockerfile_path = service_dir / '.service' / 'Dockerfile'
    
    # Copy Dockerfile to root
    subprocess.run(["cp", dockerfile_path, service_dir], check=True)
    
    try:
        subprocess.run([
            "docker", "build",
            "-t", container_name,
            str(service_dir)
        ], check=True)
    finally:
        # Clean up copied Dockerfile
        subprocess.run(["rm", service_dir / "Dockerfile"], check=True)

def __start_container(container_name: str) -> str:
    """
    Create a new Docker container without starting its main process.
    
    Args:
        container_name (str): Name of the container to create
        
    Returns:
        str: Container ID of the created container
    """
    result = subprocess.run(
        [
            "docker", "create",
            "--rm",
            container_name
        ],
        check=True,
        capture_output=True,
        text=True
    )
    
    container_id = result.stdout.strip()
    if not container_id:
        raise ValueError("Failed to get container ID from docker create command")
    
    return container_id

def __run_container(container_id: str) -> None:
    """
    Start the execution of a created Docker container.
    
    Args:
        container_id (str): ID of the container to start
    """
    subprocess.run(
        ["docker", "exec", "-it", container_id, "/bin/bash"],
        check=True
    )
    print(f"Container {container_id} execution started")

def __configure_container(container_id: str, config: Optional[dict] = None) -> None:
    """
    Configure the created container.
    
    Args:
        container_id (str): ID of the container to configure
        config (Optional[dict]): Additional configuration options
    """
    set_config(
        container_id=container_id,
        config=config,
        resources=DEFAULT_SYSTEM_RESOURCES,
        api=celaut.Service.Container.Config(path=[])
    )

def create_and_start_container(service_path: str, config: Optional[dict] = None) -> str:
    """
    Create, configure, and start a Docker container.
    
    Args:
        service_path (str): Path to the service directory
        config (Optional[dict]): Additional configuration options
        
    Returns:
        str: Container ID of the running container
    """
    try:
        # Step 1: Prepare environment
        service_dir, container_name, pre_compile = __prepare_container_environment(service_path)
        
        # Step 2: Clean up existing containers/images
        __cleanup_existing_container(container_name)
        
        # Step 3: Build the container image
        __build_container_image(service_dir, container_name)
        
        # Step 4: Create the container (but don't start it)
        container_id = __start_container(container_name)
        
        # Step 5: Configure the container
        __configure_container(container_id, config)

        # Step 6: Start the container execution
        __run_container(container_id)
        
        print(f"Container created, configured, and started successfully with ID: {container_id}")
        return container_id
        
    except subprocess.CalledProcessError as e:
        print(f"Error during Docker operation: {e}")
        raise
    except Exception as e:
        print(f"Unexpected error: {e}")
        raise

def interactive_dev_container(service_path: str) -> str:
    config = get_config(config=None, resources=None)
    write_config(path=service_path, config=config)

    # TODO Should create the container as internal service.