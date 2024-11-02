from typing import Optional
import json
import subprocess
from pathlib import Path
from protos import gateway_pb2, celaut_pb2 as celaut
from src.gateway.launcher.local_execution.set_config import set_config
from src.utils.env import DEFAULT_SYSTEM_RESOURCES

def create_dev_container(
    service_path: str
) -> str:
    """
    Build and run a Docker container with volume mounting based on pre-compile.json configuration.
    
    Args:
        service_path (str): Path to the service directory containing Dockerfile and pre-compile.json
        config (Optional[gateway_pb2.Configuration]): Configuration for the container
        resources (gateway_pb2.CombinationResources.Clause): Resource requirements
        
    Returns:
        str: The container ID of the running container
        
    Raises:
        FileNotFoundError: If required configuration files are missing
        ValueError: If workdir is not specified in configuration
        subprocess.CalledProcessError: If Docker operations fail
    """
    # Normalize and validate paths
    service_dir = Path(service_path).resolve()
    config_path = service_dir / '.service' / 'pre-compile.json'
    dockerfile_path = service_dir / '.service' / 'Dockerfile'
    
    # Validate required files exist
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    if not dockerfile_path.exists():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile_path}")
    
    # Load configuration
    with open(config_path, 'r') as file:
        pre_compile = json.load(file)
    
    # Extract workdir from config
    container_workdir = pre_compile.get('workdir')
    if not container_workdir:
        raise ValueError("Workdir not specified in pre-compile.json")
    
    # Generate container name from directory
    container_name = f"{service_dir.name}-container"
    
    try:
        # Build the Docker image
        build_cmd = [
            "docker", "build",
            "-t", container_name,
            "-f", str(dockerfile_path),
            str(service_dir / '.service')
        ]
        
        print("Building Docker image...")
        subprocess.run(build_cmd, check=True)
        
        # Run the container with volume mount in detached mode
        run_cmd = [
            "docker", "run",
            "-d",  # Run in detached mode to get container ID
            "--rm",  # Remove container after exit
            "-v", f"{service_dir}:{container_workdir}",
            container_name
        ]
        
        print("Running container with volume mount...")
        result = subprocess.run(
            run_cmd,
            check=True,
            capture_output=True,
            text=True
        )
        
        # Get container ID from output
        container_id = result.stdout.strip()
        
        if not container_id:
            raise ValueError("Failed to get container ID from docker run command")
            
        # Set configuration with container ID
        set_config(
            container_id=container_id,
            config=None,
            resources=DEFAULT_SYSTEM_RESOURCES,
            api=celaut.Service.Container.Config(path=[])
        )
        
        print(f"Container started successfully with ID: {container_id}")
        return container_id
        
    except subprocess.CalledProcessError as e:
        print(f"Error during Docker operation: {e}")
        raise
    except Exception as e:
        print(f"Unexpected error: {e}")
        raise