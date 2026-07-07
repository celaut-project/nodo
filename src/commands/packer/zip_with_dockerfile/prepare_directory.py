from typing import Tuple
import os
import subprocess
import uuid
import shutil
import json

from src.utils.config import ConfigManager

env_manager = ConfigManager()

CACHE = env_manager.get("CACHE")

def __dockerfile_copy_from(directory: str):
    dockerfile_path = os.path.join(directory, ".service", "Dockerfile")
    
    if os.path.exists(dockerfile_path):
        with open(dockerfile_path, 'r') as file:
            lines = file.readlines()
        
        with open(dockerfile_path, 'w') as file:
            for line in lines:
                if line.strip().startswith("COPY "):
                    parts = line.split()
                    # A COPY may have several context sources before the final
                    # destination: `COPY src1 src2 ... dest`. The build context is
                    # the .service/ dir (project files live under service/), so
                    # EVERY context source that starts with "." must be re-prefixed
                    # — not just the first one. `--from=<stage>` sources reference a
                    # build stage rather than the context, so leave those untouched.
                    flags = [p for p in parts[1:] if p.startswith("--")]
                    if len(parts) >= 3 and not any(
                        f.startswith("--from") for f in flags
                    ):
                        start = 1
                        while start < len(parts) and parts[start].startswith("--"):
                            start += 1
                        for j in range(start, len(parts) - 1):  # sources, not dest
                            if parts[j].startswith("."):
                                parts[j] = "service" + parts[j][1:]
                        line = " ".join(parts) + "\n"
                file.write(line)

def __ensure_is_correct(directory: str):
    service_dir = os.path.join(directory, ".service")
    dockerfile_path = os.path.join(service_dir, "Dockerfile")
    service_json_path = os.path.join(service_dir, "service.json")
    pack_config_json_path = os.path.join(service_dir, "pack_config.json")

    root_dockerfile_path = os.path.join(directory, "Dockerfile")
    root_service_json_path = os.path.join(directory, "service.json")
    root_dockerignore_path = os.path.join(directory, ".dockerignore")
    service_dockerignore_path = os.path.join(service_dir, ".dockerignore")

    # Create .service directory if it doesn't exist
    if not os.path.exists(service_dir):
        os.makedirs(service_dir)

    # Check and copy Dockerfile if it exists in the root
    if not os.path.exists(dockerfile_path) and os.path.exists(root_dockerfile_path):
        shutil.copy2(root_dockerfile_path, dockerfile_path)

    # Check and copy service.json if it exists in the root
    if not os.path.exists(service_json_path) and os.path.exists(root_service_json_path):
        shutil.copy2(root_service_json_path, service_json_path)

    # Create pack_config.json if it doesn't exist
    if not os.path.exists(pack_config_json_path):
        with open(pack_config_json_path, 'w') as f:
            f.write('{"ignore": []}')

    # Read the existing pack_config.json content
    with open(pack_config_json_path, 'r') as f:
        pack_config_data = json.load(f)

    # Ensure "ignore" key exists in pack_config.json
    if "ignore" not in pack_config_data:
        pack_config_data["ignore"] = []

    # Determine which .dockerignore file to use
    dockerignore_path = service_dockerignore_path if os.path.exists(service_dockerignore_path) else root_dockerignore_path

    # If a .dockerignore file exists, read its contents and add to the ignore list
    if os.path.exists(dockerignore_path):
        with open(dockerignore_path, 'r') as f:
            ignore_patterns = f.read().splitlines()
            pack_config_data["ignore"].extend(ignore_patterns)

    # Remove duplicates from the ignore list
    pack_config_data["ignore"] = list(set(pack_config_data["ignore"]))
    print(f"pack configuration {pack_config_data}")

    # Write the updated pack_config.json content back to the file
    with open(pack_config_json_path, 'w') as f:
        json.dump(pack_config_data, f, indent=4)
        
    __dockerfile_copy_from(directory=directory)

def prepare_directory(directory: str) -> Tuple[bool, str]:
    # Check if the directory is a remote Git repository (contains both https:// and .git)
    if "http" in directory[:4]:
        subdir = ""

        # Extract subdirectory from URL fragment
        if "#" in directory:
            directory, subdir = directory.split("#", 1)

        # If the directory URL doesn't end with ".git", append it
        if not directory.endswith(".git"):
            directory += ".git"
        
        # Construct the base name of the repository by extracting it from the URL
        repo_name = directory.split("/")[-1].replace(".git", "")
        
        # Generate a unique identifier (UUID) to avoid conflicts
        unique_id = str(uuid.uuid4())
        
        # Combine the repository name with the UUID to create a unique path
        repo_name_with_uuid = f"{repo_name}_{unique_id}"
        
        # Construct the path for the repository to be cloned into
        repo_path = os.path.join(CACHE, "git_repositories", repo_name_with_uuid)
        
        # Check if the repository already exists in the cache
        if not os.path.exists(repo_path):
            # Clone the Git repository into the cache directory
            subprocess.run(["git", "clone", directory, repo_path], check=True)
        
        # Resolve final project path
        project_path = os.path.join(repo_path, subdir) if subdir else repo_path

        # Validate subdirectory exists
        if not os.path.isdir(project_path):
            raise Exception(f"Subdirectory '{subdir}' does not exist in the repository")

        __ensure_is_correct(project_path)
        
        # Return the path to the project (NOT repo root)
        return True, project_path
    
    elif os.path.isdir(directory):
        # Remove the last character '/' from the path if it exists
        if directory[-1] == '/':
            directory = directory[:-1]
        
        repo_name = directory.split("/")[-1]
        
        # Generate a unique identifier (UUID) to avoid conflicts
        unique_id = str(uuid.uuid4())
        
        # Combine the repository name with the UUID to create a unique path
        repo_name_with_uuid = f"{repo_name}_{unique_id}"
        repo_path = os.path.join(CACHE, "local_repositories", repo_name_with_uuid)

        # Create the destination directory
        os.makedirs(repo_path, exist_ok=True)

        try:
            # Copy regular files and directories
            for item in os.listdir(directory):
                s = os.path.join(directory, item)
                d = os.path.join(repo_path, item)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
                    
            __ensure_is_correct(repo_path)
             
            return False, repo_path
            
        except subprocess.CalledProcessError as e:
            raise Exception(f"Failed to copy directory: {str(e)}")
        
    else:
        raise Exception(f"The directory {directory} must be an absolute path")
