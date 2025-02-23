import os
from src.utils.env import EnvManager
from bee_rpc.utils import getsize
from protos.celaut_pb2 import Metadata

env_manager = EnvManager()
REGISTRY = env_manager.get_env("REGISTRY")
METADATA = env_manager.get_env("METADATA_REGISTRY")

def list_services():
    # List available services in the specified registry path
    services = os.listdir(REGISTRY)
    print("Available services:\n")
    for service in services:
        # Initialize metadata for each service
        metadata = Metadata()
        
        # Try got get the tag
        try:
            # Attempt to parse the metadata from the binary file
            with open(os.path.join(METADATA, service), "rb") as f:
                metadata.ParseFromString(f.read())
            name = metadata.hashtag.tag[0] if metadata.hashtag.tag else ""
        except FileNotFoundError:
            name = ""
        except Exception:
            name = ""
            
        # Try to get the size
        try:
            size = getsize(os.path.join(REGISTRY, service))
            size = f"{size / (1024 * 1024)} MB"
        except Exception as e:
            size = f"0 - {e}"
            
        # Print.
        print(f"{service}  {name} {size}")
