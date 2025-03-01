import os
from protos.celaut_pb2 import Metadata
from src.utils.env import EnvManager

env_manager = EnvManager()
METADATA = env_manager.get_env("METADATA_REGISTRY")

def get_id(param: str) -> str:
    """
    Attempts to determine the service ID based on the provided parameter.
    It can be a direct ID, a tag (first element of tags in metadata), or another identifier.
    """
    # Check if the parameter is a direct ID
    service_path = os.path.join(METADATA, param)
    if os.path.exists(service_path):
        return param  # It is a valid ID
    
    # If it's not a direct ID, search for a tag in the metadata
    for service in os.listdir(METADATA):
        metadata_path = os.path.join(METADATA, service)
        metadata = Metadata()
        
        try:
            with open(metadata_path, "rb") as f:
                metadata.ParseFromString(f.read())
            
            # Check if the tag matches the provided parameter
            if metadata.hashtag.tag and metadata.hashtag.tag[0] == param:
                return service  # Return the found service ID
        except Exception:
            continue  # Ignore reading errors
    
    return ""  # No matching ID found
