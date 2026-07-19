import os
import shutil
from typing import Optional
from protos import celaut_pb2
from bee_rpc.client import read_from_file

from src.utils.config import ConfigManager
from src.utils.hashing import get_configured_hash_spec, hash_stream
from src.utils.service_content import read_service_content

env_manager = ConfigManager()

REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
VALIDATE_ON_IMPORT = env_manager.get("VALIDATE_ON_IMPORT")


# It's on utils.service_content too, but here we have other params...
def _compute_service_hash(service_path: str, hash_spec) -> str:
    return hash_stream(read_service_content(service_path=service_path), hash_spec).hex()


def _upsert_metadata_hash(metadata: celaut_pb2.Metadata, hash_id: bytes, hash_hex: str):
    hash_bytes = bytes.fromhex(hash_hex)
    for existing in metadata.hashtag.hash:
        if existing.type == hash_id:
            existing.value = hash_bytes
            return

    metadata.hashtag.hash.append(
        celaut_pb2.Metadata.HashTag.Hash(
            type=hash_id,
            value=hash_bytes,
        )
    )


def _remove_path(path: str):
    if not os.path.exists(path):
        return
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def import_bee(path: str, integrity_hash: Optional[str] = None) -> Optional[str]:
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
        hash_spec = get_configured_hash_spec(env_manager)

        # Read the file using bee_rpc.client
        it = read_from_file(path=path, indices={
            1: celaut_pb2.Metadata,
            2: celaut_pb2.Service,
        })
        
        # Extract the metadata directory and parse the metadata
        metadata_dir = next(it).dir
        service_dir = next(it).dir
        metadata = celaut_pb2.Metadata()
        metadata.ParseFromString(open(metadata_dir, "rb").read())
        
        # Find the configured service hash in metadata first.
        service_hash = None
        service_saved = False
        for _hash in metadata.hashtag.hash:
            if _hash.type == hash_spec.id_bytes:
                service_hash = _hash.value.hex()
                service_saved = os.path.exists(os.path.join(REGISTRY, service_hash))
                break

        if VALIDATE_ON_IMPORT:
            calculated_hash = _compute_service_hash(service_path=service_dir, hash_spec=hash_spec)

            if service_hash != calculated_hash:
                _upsert_metadata_hash(
                    metadata=metadata,
                    hash_id=hash_spec.id_bytes,
                    hash_hex=calculated_hash,
                )
                service_hash = calculated_hash
        
        if integrity_hash:
            if integrity_hash != service_hash:
                print(
                    f"Integrity hash mismatch. "
                    f"Expected: {service_hash}, received: {integrity_hash}. "
                    f"Ensure that the integrity hash uses the {hash_spec.name} algorithm."
                )
                return
        
        # Move metadata to the metadata registry
        metadata_destination = os.path.join(METADATA_REGISTRY, service_hash)
        with open(metadata_destination, "wb") as metadata_file:
            metadata_file.write(metadata.SerializeToString())
        _remove_path(metadata_dir)
        
        # Move or remove the service file based on whether it's already saved
        if not service_saved:
            service_destination = os.path.join(REGISTRY, service_hash)
            shutil.move(service_dir, service_destination)
        else:
            _remove_path(service_dir)
        
        print("\n\nService imported successfully.")
        return service_hash
    
    except Exception as e:
        print(f"Error importing service: {e}")
        return
