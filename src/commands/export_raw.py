import os
from typing import Generator, Any
from bee_rpc.reader import read_from_registry
from src.commands.__by_tag import get_id
from src.utils.config import ConfigManager
from protos import celaut_pb2

# Initialize the environment manager and get the REGISTRY environment variable
env_manager = ConfigManager()
REGISTRY = env_manager.get("REGISTRY")

def export_raw(service: str, path: str):
    """
    Export data from the specified service in the registry and save it to a file.

    Args:
        service (str): The name of the service to read data from.
        path (str): The directory path where the output file should be saved.
    """
    # Get the current directory (where the "nodo" command is executed from)
    current_directory = os.getcwd()
    
    # Resolve the path: if it's relative, combine it with the current directory
    if not os.path.isabs(path):
        path = os.path.join(current_directory, path)
    
    # Convert the path to absolute (just in case)
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
    
    output_file = f"{path}/{file_name}.celaut"

    try:
        # Write the service data to the output file
        with open(output_file, "wb+") as file:
            for chunk in read_from_registry(filename=os.path.join(REGISTRY, service_id)):
                file.write(chunk.chunk)

        print(f"Export completed: {output_file}")
    
    except Exception as e:
        print(f"Error exporting service {service[:6]}: {e}")
