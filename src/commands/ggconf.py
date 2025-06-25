from src.virtualizers.docker.set_container_config import write_config
from src.virtualizers.docker.set_container_config import get_config
from src.commands.__by_tag import get_id
import json
import os
import shutil
from typing import Dict
from pathlib import Path
from src.utils.env import EnvManager

from src.commands.packer.zip_with_dockerfile.generate_service_zip import (
    SERVICE_DEPENDENCIES_DIRECTORY,
    METADATA_DEPENDENCIES_DIRECTORY,
    BLOCKS_DIRECTORY,
    DEPENDENCIES_DIR,
    SKIP_WBP,
    DEPENDENCIES_ENV
)

env_manager = EnvManager()

METADATA = env_manager.get_env("METADATA_REGISTRY")
SERVICES = env_manager.get_env("REGISTRY")
BLOCKS = env_manager.get_env("BLOCKDIR")


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

    # Write the .dependencies file in the service's root directory
    dependencies_file_path = Path(path) / ".dependencies"
    print(f"Generating dependencies file at: '{dependencies_file_path}'")
    with open(dependencies_file_path, "w") as f:
        for env, dep_hash in resolved_deps.items():
            f.write(f"{env}={dep_hash}\n")
            
    print("INFO: Development .dependencies file generated successfully.")


def generate_gateway_config_dev(path: str):
    """
    Generates a gateway configuration and the dependencies file for development.

    Args:
        path (str): Path to the service directory.
    """
    path = path.rstrip('/')

    config_dir_path = Path(path) / "__config__"
    if not config_dir_path.exists():
        print("Creating configuration file for development...")
        os.makedirs(path, exist_ok=True)
        config= get_config(config=None, resources=None)
        write_config(path=path, config=config)
    else:
        print("INFO: The '__config__' file already exists.")

    _generate_dev_dependencies(path)
    
    print(f"\nDevelopment environment setup finished for the service at: '{path}'")
