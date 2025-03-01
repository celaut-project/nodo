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
            size = getsize(os.path.join(REGISTRY, service))  # TODO This should be taken with block size too.
            size = f"{size / (1024 * 1024)} MB"
        except Exception as e:
            size = f"0 - {e}"
            
        # Print.
        print(f"{service}  {name} {size}")

def inspect(id: str):
    metadata = Metadata()
    
    # service = Service()  
    
    # TODO This should print all the wbp. ... but only when all the filesystem is outside of it, on blocks.

def modify_tag(id: str, tag: str):
    metadata = Metadata()
    
    # Path to the metadata file for this service
    metadata_path = os.path.join(METADATA, id)
    
    # Try to load existing metadata if it exists
    try:
        with open(metadata_path, "rb") as f:
            metadata.ParseFromString(f.read())
    except FileNotFoundError:
        # If file doesn't exist, we'll create new metadata with just this tag
        metadata.hashtag.tag.append(tag)
    except Exception as e:
        print(f"Error reading metadata: {e}")
        return

    # Modify only the first tag
    if metadata.hashtag.tag:  # If there are existing tags
        metadata.hashtag.tag[0] = tag  # Replace the first one
    else:
        metadata.hashtag.tag.append(tag)  # Add as first tag if none exist
    
    # Save the updated metadata back to file
    try:
        with open(metadata_path, "wb") as f:
            f.write(metadata.SerializeToString())
        print(f"Successfully updated first tag for {id} to '{tag}'")
    except Exception as e:
        print(f"Error saving metadata: {e}")