import json
import os
import shutil
from typing import Dict
from src.utils.config import ConfigManager

env_manager = ConfigManager()

METADATA = env_manager.get("METADATA_REGISTRY")
SERVICES = env_manager.get("REGISTRY")
BLOCKS = env_manager.get("BLOCKDIR")

# Pack-config json keys of service storage directories.
SERVICE_DEPENDENCIES_DIRECTORY = "service_dependencies_directory"
METADATA_DEPENDENCIES_DIRECTORY = "metadata_dependencies_directory"
BLOCKS_DIRECTORY = "blocks_directory"
DEPENDENCIES_DIR = "dependencies"
SKIP_WBP = "ignore_loadable_protobuf"
DEPENDENCIES_ENV = "dependencies_env"

# Default directory names used when the corresponding pack_config keys are
# omitted. These match the values documented in docs/PACKING.md, so the
# "Required: No" contract holds even when `dependencies` are declared.
DEFAULT_SERVICE_DEPENDENCIES_DIRECTORY = "__services__"
DEFAULT_METADATA_DEPENDENCIES_DIRECTORY = "__metadata__"
DEFAULT_BLOCKS_DIRECTORY = "__block__"


def __export_registry(project_dir: str, directory: str, pack_config: Dict):
    # Resolve the dependency-directory names. These keys are optional in
    # pack_config.json (see docs/PACKING.md); when omitted we fall back to the
    # documented default directory names instead of raising a KeyError.
    service_deps_dir = pack_config.get(SERVICE_DEPENDENCIES_DIRECTORY, DEFAULT_SERVICE_DEPENDENCIES_DIRECTORY)
    metadata_deps_dir = pack_config.get(METADATA_DEPENDENCIES_DIRECTORY, DEFAULT_METADATA_DEPENDENCIES_DIRECTORY)
    blocks_dir = pack_config.get(BLOCKS_DIRECTORY, DEFAULT_BLOCKS_DIRECTORY)

    list(map(
        lambda _dir: os.makedirs(f"{directory}/{_dir}", exist_ok=True)
            if type(_dir) is str else 1,
        [
            service_deps_dir,
            metadata_deps_dir,
            blocks_dir
        ]
    ))

    if DEPENDENCIES_DIR in pack_config:
        skip_wbp = pack_config[SKIP_WBP] if SKIP_WBP in pack_config else False  # By default, will be included.
        write_env = pack_config[DEPENDENCIES_ENV] if DEPENDENCIES_ENV in pack_config else False  # Write a file with the final hashes for the case where some dependencies need to be packed too.
        dest_dir = f"{directory}/{service_deps_dir}"
        
        if type(pack_config[DEPENDENCIES_DIR]) is dict:
            dependencies = pack_config[DEPENDENCIES_DIR]
        else: 
            dependencies = dict(enumerate(pack_config[DEPENDENCIES_DIR]))
            if write_env:
                raise Exception(f"Without keys, write env doesn't have sense. Provide keys for dependencies or make dependencies_env=false: {pack_config}")
                       
        for env, dependency in dependencies.items():

            # Move dependency service.
            if not os.path.exists(f"{SERVICES}/{dependency}"):
                print(f"The dependency {dependency} does not exists.")
                # Maybe it's a path or git repo url.
                if "http" in dependency:
                    _dir = dependency
                
                else:
                    # Maybe it's a path, in that case, it will be a relative path from the repo root path.
                    _dir = os.path.join(project_dir, dependency)
                    
                if _dir:
                    print(f"Go to pack {dependency}")
                    from src.commands.packer.zip_with_dockerfile.pack import pack
                    try:
                        dependency = pack(_dir)
                    except Exception as e:
                        dependency = None
                    
                    if not dependency:
                        raise Exception(f"Dependency packing error. Process cancelled.")
                
                else:
                    raise Exception(f"Dependency not found. {dependency}")
            
            os.system(f"cp -R {SERVICES}/{dependency} {dest_dir}")
            
            if skip_wbp:
                wbp_path = os.path.join(dest_dir, dependency, "wbp.bin")
                if os.path.exists(wbp_path):
                    os.remove(wbp_path)

            # Move dependency's metadata
            if os.path.exists(f"{METADATA}/{dependency}"):
                os.system(f"cp -R {METADATA}/{dependency} "
                          f"{directory}/{metadata_deps_dir}")

            # Move dependency's blocks.
            if os.path.isdir(f"{SERVICES}/{dependency}"):
                with open(f"{SERVICES}/{dependency}/_.json", 'r') as dependency_json_file:
                    dependency_json = json.load(dependency_json_file)
                    for _e in dependency_json:
                        if type(_e) == list:
                            block: str = _e[0]
                            if not os.path.exists(
                                    f'{directory}/{blocks_dir}/{block}'
                            ):
                                os.system(f"cp -r {BLOCKS}/{block} "
                                          f"{directory}/{blocks_dir}")

            # Write env
            if write_env:
                with open(os.path.join(directory, ".dependencies"), "a") as f:
                    f.write(f"{env}={dependency}\n")

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
    config_path = f'{project_directory}/.service/pack_config.json'
    if os.path.exists(config_path):
        with open(config_path, 'r') as config_file:
            pack_config = json.load(config_file)
    else:
        pack_config = {}

    # Copy the project files to the complete_source_directory.
    if 'include' in pack_config:
        for item in pack_config['include']:
            src_path = os.path.join(project_directory, item)
            dest_path = os.path.join(complete_source_directory, item)
            
            dest_parent = os.path.dirname(dest_path)
            os.makedirs(dest_parent, exist_ok=True)
            
            if os.path.isdir(src_path):
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
            else:
                shutil.copy2(src_path, dest_path)
            print(f"Added file {item}")
            
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
    if 'ignore' in pack_config:
        for file in pack_config['ignore']:
            os.system(f"cd {complete_source_directory} && rm -rf {file}")

    # Add the dependencies
    __export_registry(project_dir=project_directory, directory=complete_source_directory, pack_config=pack_config)

    if 'zip' in pack_config and pack_config['zip']:
        # Dependency-directory keys are optional; fall back to the documented
        # default directory names (matching __export_registry) when omitted.
        service_deps_dir = pack_config.get(SERVICE_DEPENDENCIES_DIRECTORY, DEFAULT_SERVICE_DEPENDENCIES_DIRECTORY)
        metadata_deps_dir = pack_config.get(METADATA_DEPENDENCIES_DIRECTORY, DEFAULT_METADATA_DEPENDENCIES_DIRECTORY)
        blocks_dir = pack_config.get(BLOCKS_DIRECTORY, DEFAULT_BLOCKS_DIRECTORY)
        os.system(f'cd {complete_source_directory} && '
                  f'zip -r services.zip'
                  f' {service_deps_dir}'
                  f' {metadata_deps_dir}'
                  f' {blocks_dir}')
        os.system(f'cd {complete_source_directory} && '
                  f'rm -rf {service_deps_dir} '
                  f'{metadata_deps_dir} '
                  f'{blocks_dir}')

    # Create a ZIP file of the destination source directory
    os.system(f"cd {project_directory}/.service && zip -r .service.zip .")

    # Remove the destination source directory
    os.system(f"rm -rf {complete_source_directory}")

    # Return the path of the generated ZIP file
    return project_directory + '/.service/.service.zip'
