import json
import os
import shutil
from typing import Dict
from src.utils.env import EnvManager

env_manager = EnvManager()

METADATA = env_manager.get_env("METADATA_REGISTRY")
SERVICES = env_manager.get_env("REGISTRY")
BLOCKS = env_manager.get_env("BLOCKDIR")

# Pre-compile json keys of service storage directories.
SERVICE_DEPENDENCIES_DIRECTORY = "service_dependencies_directory"
METADATA_DEPENDENCIES_DIRECTORY = "metadata_dependencies_directory"
BLOCKS_DIRECTORY = "blocks_directory"
DEPENDENCIES_DIR = "dependencies"
SKIP_WBP = "ignore_loadable_protobuf"


def __export_registry(directory: str, compile_config: Dict):
    list(map(
        lambda _reg: os.makedirs(f"{directory}/{compile_config[_reg]}")
            if _reg in compile_config and type(compile_config[_reg]) is str else 1,
        [
            SERVICE_DEPENDENCIES_DIRECTORY,
            METADATA_DEPENDENCIES_DIRECTORY,
            BLOCKS_DIRECTORY
        ]
    ))

    if DEPENDENCIES_DIR in compile_config:
        skip_wbp = compile_config[SKIP_WBP] if SKIP_WBP in compile_config else False  # By default, will be included.
        dest_dir = f"{directory}/{compile_config[SERVICE_DEPENDENCIES_DIRECTORY]}"
        for dependency in compile_config[DEPENDENCIES_DIR].values() \
                if type(compile_config[DEPENDENCIES_DIR]) is dict else compile_config[DEPENDENCIES_DIR]:

            # Move dependency service.
            if not os.path.exists(f"{SERVICES}/{dependency}"):
                raise Exception(f"Dependency not found. {dependency}")
            
            os.system(f"cp -R {SERVICES}/{dependency} {dest_dir}")
            
            if skip_wbp:
                wbp_path = os.path.join(dest_dir, dependency, "wbp.bin")
                if os.path.exists(wbp_path):
                    os.remove(wbp_path)

            # Move dependency's metadata
            if os.path.exists(f"{METADATA}/{dependency}"):
                os.system(f"cp -R {METADATA}/{dependency} "
                          f"{directory}/{compile_config[METADATA_DEPENDENCIES_DIRECTORY]}")

            # Move dependency's blocks.
            if os.path.isdir(f"{SERVICES}/{dependency}"):
                with open(f"{SERVICES}/{dependency}/_.json", 'r') as dependency_json_file:
                    dependency_json = json.load(dependency_json_file)
                    for _e in dependency_json:
                        if type(_e) == list:
                            block: str = _e[0]
                            if not os.path.exists(
                                    f'{directory}/{compile_config[BLOCKS_DIRECTORY]}/{block}'
                            ):
                                os.system(f"cp -r {BLOCKS}/{block} "
                                          f"{directory}/{compile_config[BLOCKS_DIRECTORY]}")

def generate_service_zip(project_directory: str) -> str:
    
    # Remove the last character '/' from the path if it exists
    if project_directory[-1] == '/':
        project_directory = project_directory[:-1]

    # Remove the ZIP file and the destination source directory if they already exist
    os.system(f"cd {project_directory}/.service && rm .service.zip && rm -rf service")

    # Define the complete path for the destination source directory
    complete_source_directory = f"{project_directory}/.service/service"

    # Create the destination source directory and copy all files and folders from the project there
    os.system(f"mkdir {complete_source_directory}")

    # Read the compilation's config JSON file
    config_path = f'{project_directory}/.service/pre-compile.json'
    if os.path.exists(config_path):
        with open(config_path, 'r') as config_file:
            compile_config = json.load(config_file)
    else:
        compile_config = {}

    # Copy the project files to the complete_source_directory.
    if 'include' in compile_config:
        for item in compile_config['include']:
            src_path = os.path.join(project_directory, item)
            dest_path = os.path.join(complete_source_directory, item)
            if os.path.isdir(src_path):
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
            else:
                shutil.copy2(src_path, dest_path)
    else:
        for item in os.listdir(project_directory):
            if item == ".service": continue
            src_path = os.path.join(project_directory, item)
            dest_path = os.path.join(complete_source_directory, item)
            if os.path.isdir(src_path):
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
            else:
                shutil.copy2(src_path, dest_path)

    # Remove the files and directories specified in the "ignore" list from the configuration
    if 'ignore' in compile_config:
        for file in compile_config['ignore']:
            os.system(f"cd {complete_source_directory} && rm -rf {file}")

    # Add the dependencies
    __export_registry(directory=complete_source_directory, compile_config=compile_config)

    if 'zip' in compile_config and compile_config['zip']:
        os.system(f'cd {complete_source_directory} && '
                  f'zip -r services.zip'
                  f' {compile_config[SERVICE_DEPENDENCIES_DIRECTORY]}'
                  f' {compile_config[METADATA_DEPENDENCIES_DIRECTORY]}'
                  f' {compile_config[BLOCKS_DIRECTORY]}')
        os.system(f'cd {complete_source_directory} && '
                  f'rm -rf {compile_config[SERVICE_DEPENDENCIES_DIRECTORY]} '
                  f'{compile_config[METADATA_DEPENDENCIES_DIRECTORY]} '
                  f'{compile_config[BLOCKS_DIRECTORY]}')

    # Create a ZIP file of the destination source directory
    os.system(f"cd {project_directory}/.service && zip -r .service.zip .")

    # Remove the destination source directory
    os.system(f"rm -rf {complete_source_directory}")

    # Return the path of the generated ZIP file
    return project_directory + '/.service/.service.zip'
