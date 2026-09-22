import os
from src.commands.__by_tag import get_id
from src.utils.config import ConfigManager
from bee_rpc.utils import getsize
from protos.celaut_pb2 import Metadata

env_manager = ConfigManager()
REGISTRY = env_manager.get("REGISTRY")
METADATA = env_manager.get("METADATA_REGISTRY")

def list_services():
    # List available services in the specified registry path
    services = os.listdir(REGISTRY)
    for service in services:
        # Initialize metadata for each service
        metadata = Metadata()
        
        # Try got get the tag
        try:  # TODO check. Repeated on instances.get_tag function.
            # Attempt to parse the metadata from the binary file
            with open(os.path.join(METADATA, service), "rb") as f:
                metadata.ParseFromString(f.read())
            name = metadata.hashtag.tag[0] if metadata.hashtag.tag else ""
        except FileNotFoundError:
            name = ""
        except Exception:
            name = ""
            
        # What the service weighs, blocks included.
        #
        # `getsize` already totals both halves -- the parts stored in the service's
        # own directory, and the content-addressed blocks it references -- so the
        # old TODO here was already satisfied by the call it was written above.
        # What was missing is that the two are different questions: the blocks are
        # shared with every other service referencing the same bytes, so the total
        # is what this service *is*, and the directory is what it *adds* to the
        # disk. Both are printed, because a service that stores 64 bytes and weighs
        # 8 GiB is neither figure on its own.
        try:
            total = getsize(os.path.join(REGISTRY, service))
            stored = sum(
                os.path.getsize(os.path.join(dirpath, name))
                for dirpath, _, names in os.walk(os.path.join(REGISTRY, service))
                for name in names
            )
            size = f"{total / (1024 * 1024):.2f} MB ({stored / (1024 * 1024):.2f} MB stored here)"
        except Exception as e:
            size = f"0 - {e}"
            
        # Print.
        print(f"{service}  {size} {name}")

def modify_tag(service: str, tag: str):
    service = get_id(service)
    
    metadata = Metadata()
    
    # Path to the metadata file for this service
    metadata_path = os.path.join(METADATA, service)
    
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
        print(f"Successfully updated first tag for {service} to '{tag}'")
    except Exception as e:
        print(f"Error saving metadata: {e}")