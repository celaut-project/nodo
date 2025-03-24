import os
from typing import Optional
from protos import celaut_pb2
from bee_rpc.client import read_from_file

from src.gateway.iterables.abstract_service_iterable import find_service_hash
from src.utils.env import EnvManager

env_manager = EnvManager()

REGISTRY = env_manager.get_env("REGISTRY")
METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")


def import_bee(path: str) -> Optional[str]:
    # Get the current directory (where the "nodo" command is executed from)
    current_directory = os.getcwd()
    
    # Resolve the path: if it's relative, combine it with the current directory
    if not os.path.isabs(path):
        path = os.path.join(current_directory, path)
    
    # Convert the path to absolute (just in case)
    path = os.path.abspath(path)
    
    # Validate if the path exists
    if not os.path.exists(path):
        print(f"Error: The path '{path}' does not exist.")
        return
    
    # Validate if the path is a file
    if not os.path.isfile(path):
        print(f"Error: The path '{path}' is not a file.")
        return
    
    try:
        # Read the file using bee_rpc.client
        it = read_from_file(path=path, indices={
            1: celaut_pb2.Metadata,
            2: celaut_pb2.Service,
        })
        
        # Extract the metadata directory and parse the metadata
        metadata_dir = next(it).dir
        metadata = celaut_pb2.Metadata()
        metadata.ParseFromString(open(metadata_dir, "rb").read())
        
        # Find the service hash in the metadata
        service_hash = None
        for _hash in metadata.hashtag.hash:
            service_hash, service_saved = find_service_hash(_hash=_hash)
            break
                
        if not service_hash:
            print("Error: The .celaut file does not contain a service hash. Implement the task: https://github.com/celaut-project/nodo/issues/47")
            return
        
        # Move metadata to the metadata registry
        metadata_destination = os.path.join(METADATA_REGISTRY, service_hash)
        os.system(f"mv {metadata_dir} {metadata_destination}")
        
        # Move or remove the service file based on whether it's already saved
        service_dir = next(it).dir
        if not service_saved:
            service_destination = os.path.join(REGISTRY, service_hash)
            os.system(f"mv {service_dir} {service_destination}")
        else:
            os.system(f"rm -rf {service_dir}")
        
        print("Service imported successfully.")
        return service_hash
    
    except Exception as e:
        print(f"Error importing service: {e}")
