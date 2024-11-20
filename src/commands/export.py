import os
from grpcbigbuffer.reader import read_from_registry
from src.utils.env import EnvManager

# Initialize the environment manager and get the REGISTRY environment variable
env_manager = EnvManager()
REGISTRY = env_manager.get_env("REGISTRY")
METADATA = env_manager.get_env("METADATA_REGISTRY")

def export(service: str, path: str):
    """
    Export data from the specified service in the registry and save it to a file.

    Args:
        service (str): The name of the service to read data from.
        path (str): The directory path where the output file should be saved.
    """
    # Create the full path for the output file
    output_file = os.path.join(path, f"{service}.bee")  # bee-rpc file extension

    # Ensure the output directory exists
    os.makedirs(path, exist_ok=True)

    # Open the output file in write-binary mode
    with open(output_file, 'wb') as f:
        
        # Read chunks of data from the registry and write them to the file
        for buff in read_from_registry(filename=os.path.join(METADATA, service)):
            f.write(buff.SerializeToString())
        
        # Read chunks of data from the registry and write them to the file
        for buff in read_from_registry(filename=os.path.join(REGISTRY, service)):
            f.write(buff.SerializeToString())

    print(f"Export completed.")
