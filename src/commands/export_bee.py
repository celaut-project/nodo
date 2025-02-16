import os
from typing import Generator, Any
from bee_rpc.client import write_to_file, Dir
from src.utils.env import EnvManager
from protos import celaut_pb2

# Initialize the environment manager and get the REGISTRY environment variable
env_manager = EnvManager()
REGISTRY = env_manager.get_env("REGISTRY")
METADATA = env_manager.get_env("METADATA_REGISTRY")

def __generator(service: str) -> Generator[Any, None, None]:
    try:

        yield Dir(
            dir=os.path.join(METADATA, service),
            _type=celaut_pb2.Any.Metadata
        )

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
    
    output_file = write_to_file(
        path=path, 
        file_name=service[:6], 
        extension="celaut",
        input=__generator(service=service), 
        indices={
            1: celaut_pb2.Any.Metadata,
            2: celaut_pb2.Service,
        })

    print(f"Export completed {output_file}")
