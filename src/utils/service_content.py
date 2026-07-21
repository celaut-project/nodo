import os
from pathlib import Path
from typing import Generator, Union
from bee_rpc.client import read_multiblock_directory

from src.utils.config import ConfigManager
from src.utils.hashing import get_configured_hash_spec, hash_stream

env_manager = ConfigManager()
REGISTRY = env_manager.get("REGISTRY")

def read_service_content(service_path: Union[str, Path]) -> Generator[bytes, None, None]:
    """
    Reads the content of a service (either a file or a multiblock directory)
    in chunks of 1MB, yielding them as bytes.
    """
    path_str = str(service_path)
    if os.path.isfile(path_str):
        with open(path_str, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                yield chunk
    else:
        for chunk in read_multiblock_directory(directory=path_str):
            yield chunk

def compute_id(service_id: str) -> str:
    service_path = os.path.join(REGISTRY, service_id)
    hash_spec = get_configured_hash_spec(env_manager)
    return hash_stream(read_service_content(service_path=service_path), hash_spec).hex()
