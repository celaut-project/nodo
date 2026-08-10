from src.database.sql_connection import SQLConnection
from src.manager.manager import get_dev_clients
from src.utils.configuration_file import write_config
from src.utils.configuration_file import get_config
from src.commands.__by_tag import get_id
import json
import os
import socket
from typing import Dict
from pathlib import Path
from src.utils.config import ConfigManager
from protos.celaut_pb2 import Configuration

from src.commands.packer.zip_with_dockerfile.generate_service_zip import (
    SERVICE_DEPENDENCIES_DIRECTORY,
    METADATA_DEPENDENCIES_DIRECTORY,
    BLOCKS_DIRECTORY,
    DEPENDENCIES_DIR,
    SKIP_WBP,
    DEPENDENCIES_ENV
)

env_manager = ConfigManager()

METADATA = env_manager.get("METADATA_REGISTRY")
SERVICES = env_manager.get("REGISTRY")
BLOCKS = env_manager.get("BLOCKDIR")

sc = SQLConnection()


def _generate_dev_dependencies(path: str):
    """
    Generates the .dependencies file for the development environment.
    It resolves dependencies from the registry or by packing local paths.
    """
    print("Attempting to generate development dependencies file...")
    
    # Build the path to the package configuration file
    config_path = Path(path) / '.service' / 'pack_config.json'
    if not config_path.exists():
        # Configuration might be in the root for some legacy projects.
        config_path = Path(path) / 'pack_config.json'
        if not config_path.exists():
            print("INFO: 'pack_config.json' not found. Skipping dependency generation.")
            return

    with open(config_path, 'r') as config_file:
        pack_config = json.load(config_file)

    # Check if the setting to generate the dependencies .env is active
    if DEPENDENCIES_DIR not in pack_config or not pack_config.get(DEPENDENCIES_ENV, False):
        print(f"INFO: '{DEPENDENCIES_DIR}' not found or '{DEPENDENCIES_ENV}' is false in 'pack_config.json'. Skipping.")
        return

    # Dependencies must be a dictionary for environment variable mapping
    if not isinstance(pack_config[DEPENDENCIES_DIR], dict):
        raise TypeError(
            f"For development dependency generation, the '{DEPENDENCIES_DIR}' key "
            f"must be a dictionary (key: value). Provide keys or set '{DEPENDENCIES_ENV}' to false."
        )
        
    dependencies = pack_config[DEPENDENCIES_DIR]
    resolved_deps = {}

    list(map(
        lambda _reg: os.makedirs(f"{path}/{pack_config[_reg]}")
            if _reg in pack_config and type(pack_config[_reg]) is str else 1,
        [
            SERVICE_DEPENDENCIES_DIRECTORY,
            METADATA_DEPENDENCIES_DIRECTORY,
            BLOCKS_DIRECTORY
        ]
    ))

    skip_wbp = pack_config[SKIP_WBP] if SKIP_WBP in pack_config else False  # By default, will be included.
    write_env = pack_config[DEPENDENCIES_ENV] if DEPENDENCIES_ENV in pack_config else False  # Write a file with the final hashes for the case where some dependencies need to be packed too.
    dest_dir = f"{path}/{pack_config[SERVICE_DEPENDENCIES_DIRECTORY]}"

    print("Resolving dependencies...")
    for env, dependency in dependencies.items():
        dependency = get_id(dependency)

        # Check if the dependency already exists in the services registry
        if not os.path.exists(f"{SERVICES}/{dependency}"):
            raise Exception(
                f"Dependency '{dependency}' not found in the services registry at '{SERVICES}'. "
                "Ensure the dependency is available locally."
            )
        else:
            # The dependency already exists in the registry, use its name/hash
            print(f"OK: Dependency '{dependency}' found in registry.")
            resolved_deps[env] = dependency

        os.system(f"cp -R {SERVICES}/{dependency} {dest_dir}")

        if skip_wbp:
            wbp_path = os.path.join(dest_dir, dependency, "wbp.bin")
            if os.path.exists(wbp_path):
                os.remove(wbp_path)

        # Move dependency's metadata
        if os.path.exists(f"{METADATA}/{dependency}"):
            os.system(f"cp -R {METADATA}/{dependency} "
                        f"{path}/{pack_config[METADATA_DEPENDENCIES_DIRECTORY]}")

        # Move dependency's blocks.
        if os.path.isdir(f"{SERVICES}/{dependency}"):
            with open(f"{SERVICES}/{dependency}/_.json", 'r') as dependency_json_file:
                dependency_json = json.load(dependency_json_file)
                for _e in dependency_json:
                    if type(_e) == list:
                        block: str = _e[0]
                        if not os.path.exists(
                                f'{path}/{pack_config[BLOCKS_DIRECTORY]}/{block}'
                        ):
                            os.system(f"cp -r {BLOCKS}/{block} "
                                        f"{path}/{pack_config[BLOCKS_DIRECTORY]}")

    # Write the .dependencies file in the service's root directory
    dependencies_file_path = Path(path) / ".dependencies"
    print(f"Generating dependencies file at: '{dependencies_file_path}'")
    with open(dependencies_file_path, "w") as f:
        for env, dep_hash in resolved_deps.items():
            f.write(f"{env}={dep_hash}\n")
            
    print("INFO: Development .dependencies file generated successfully.")

def get_local_ip():
    # Creates an UDP socket to determine the local IP address.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Not needed that 8.8.8.8 is reachable, just to get the local IP
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ip

def generate_gateway_config_dev(path: str, envs: Dict[str, str]):
    """
    Generates a gateway configuration and the dependencies file for development.

    Args:
        path (str): Path to the service directory.
    """
    path = path.rstrip('/')

    # Add the configuration file
    config_dir_path = Path(path) / "__config__"
    if not config_dir_path.exists():
        print("Creating configuration file for development...")
        os.makedirs(path, exist_ok=True)
        config = Configuration()
        if envs:
            config.environment_variables.update({
                k: v.encode() for k, v in envs.items()
            })
        config= get_config(config=config, resources=None)
        print(f"Writing the configuration {config} to the path: '{path}'")
        write_config(path=path, config=config)
    else:
        print("INFO: The '__config__' file already exists.")


    # Generate dependencies
    _generate_dev_dependencies(path)


    # Add local instance into the DB

    # Unmetered dev client for the ggconf sandbox; the balance only has to clear
    # whatever the node quotes.
    balance_mu = 10**16

    client_id = next(get_dev_clients(amount_mu=balance_mu))

    sc.add_local_instance(
        name="rundev::" + path,
        father_id=client_id,
        container_id="rundev::" + path + "::" + str(os.getpid()),
        container_ip=get_local_ip(),  # localhost
        balance_mu=balance_mu,
        serialized_instance="",
        service_id="rundev::" + path,
        virtualizer="ch",
        disk_space=0, # TODO
        envs=""
    )
    
    print(f"\nDevelopment environment setup finished for the service at: '{path}'")
