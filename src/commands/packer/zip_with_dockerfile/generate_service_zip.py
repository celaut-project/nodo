import json
import os
import pathlib
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


def __export_registry(project_dir: str, directory: str, pack_config: Dict):
    list(map(
        lambda _reg: os.makedirs(f"{directory}/{pack_config[_reg]}")
            if _reg in pack_config and type(pack_config[_reg]) is str else 1,
        [
            SERVICE_DEPENDENCIES_DIRECTORY,
            METADATA_DEPENDENCIES_DIRECTORY,
            BLOCKS_DIRECTORY
        ]
    ))

    if DEPENDENCIES_DIR in pack_config:
        skip_wbp = pack_config[SKIP_WBP] if SKIP_WBP in pack_config else False  # By default, will be included.
        write_env = pack_config[DEPENDENCIES_ENV] if DEPENDENCIES_ENV in pack_config else False  # Write a file with the final hashes for the case where some dependencies need to be packed too.
        dest_dir = f"{directory}/{pack_config[SERVICE_DEPENDENCIES_DIRECTORY]}"
        
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
                          f"{directory}/{pack_config[METADATA_DEPENDENCIES_DIRECTORY]}")

            # Move dependency's blocks.
            if os.path.isdir(f"{SERVICES}/{dependency}"):
                with open(f"{SERVICES}/{dependency}/_.json", 'r') as dependency_json_file:
                    dependency_json = json.load(dependency_json_file)
                    for _e in dependency_json:
                        if type(_e) == list:
                            block: str = _e[0]
                            if not os.path.exists(
                                    f'{directory}/{pack_config[BLOCKS_DIRECTORY]}/{block}'
                            ):
                                os.system(f"cp -r {BLOCKS}/{block} "
                                          f"{directory}/{pack_config[BLOCKS_DIRECTORY]}")

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

    # Remove the files and directories specified in the "ignore" list from the configuration.
    # Recursive, .dockerignore-like exclusion done in Python (no shell). Each pattern is matched
    # against every path under the packaged source dir via rglob, so nested matches (e.g. src/foo.pyc,
    # src/__pycache__/) are excluded too. Blank lines, comments (#) and negations (!) — which get merged
    # in raw from .dockerignore — are skipped, and trailing-slash directory patterns are tolerated.
    if 'ignore' in pack_config:
        source_root = pathlib.Path(complete_source_directory)
        for pattern in pack_config['ignore']:
            pattern = pattern.strip()
            if not pattern or pattern.startswith(("#", "!")):
                continue
            pattern = pattern.rstrip("/")  # "__pycache__/" -> "__pycache__"
            if not pattern:
                continue
            for p in source_root.rglob(pattern):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)

    # Add the dependencies
    __export_registry(project_dir=project_directory, directory=complete_source_directory, pack_config=pack_config)

    if 'zip' in pack_config and pack_config['zip']:
        os.system(f'cd {complete_source_directory} && '
                  f'zip -r services.zip'
                  f' {pack_config[SERVICE_DEPENDENCIES_DIRECTORY]}'
                  f' {pack_config[METADATA_DEPENDENCIES_DIRECTORY]}'
                  f' {pack_config[BLOCKS_DIRECTORY]}')
        os.system(f'cd {complete_source_directory} && '
                  f'rm -rf {pack_config[SERVICE_DEPENDENCIES_DIRECTORY]} '
                  f'{pack_config[METADATA_DEPENDENCIES_DIRECTORY]} '
                  f'{pack_config[BLOCKS_DIRECTORY]}')

    # Create a ZIP file of the destination source directory
    os.system(f"cd {project_directory}/.service && zip -r .service.zip .")

    # Remove the destination source directory
    os.system(f"rm -rf {complete_source_directory}")

    # Return the path of the generated ZIP file
    return project_directory + '/.service/.service.zip'
