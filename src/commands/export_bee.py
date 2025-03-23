import os
from typing import Generator, Any
from bee_rpc.client import write_to_file, Dir
from src.commands.__by_tag import get_id
from src.utils.env import EnvManager
from protos import celaut_pb2

# Initialize the environment manager and get the REGISTRY environment variable
env_manager = EnvManager()
REGISTRY = env_manager.get_env("REGISTRY")
METADATA = env_manager.get_env("METADATA_REGISTRY")

def __generator(service: str) -> Generator[Any, None, None]:
    try:
        # Yield metadata directory
        yield Dir(
            dir=os.path.join(METADATA, service),
            _type=celaut_pb2.Metadata
        )

        # Yield service directory
        yield Dir(
            dir=os.path.join(REGISTRY, service),
            _type=celaut_pb2.Service
        )

    except Exception as e:
        print(f"Exception on exporting {service[:6]}: {e}")

def export_bee(service: str, path: str):
    """
    Export data from the specified service in the registry and save it to a file.

    Args:
        service (str): The name of the service to read data from.
        path (str): The directory path where the output file should be saved.
    """
    # Convert relative path to absolute path
    path = os.path.abspath(path)
    
    # Validate if the output directory exists
    if not os.path.exists(path):
        print(f"Error: The output directory '{path}' does not exist.")
        return
    
    # Validate if the path is a directory
    if not os.path.isdir(path):
        print(f"Error: The path '{path}' is not a directory.")
        return
    
    # Get the service ID
    service_id = get_id(service)
    
    # Generate the output file name
    file_name = service if service != service_id else service_id[:6]
    file_name = file_name.replace("-", "_")  # Replace hyphens with underscores
    
    try:
        # Write the service data to the output file
        output_file = write_to_file(
            path=path,
            file_name=file_name,
            extension="celaut",
            input=__generator(service=service_id), 
            indices={
                1: celaut_pb2.Metadata,
                2: celaut_pb2.Service,
            }
        )
        
        print(f"Export completed: {output_file}")
    
    except Exception as e:
        print(f"Error exporting service {service[:6]}: {e}")