from typing import Tuple
from src.utils.env import EnvManager
import os
import subprocess
import uuid

env_manager = EnvManager()

CACHE = env_manager.get_env("CACHE")

def in_case_of_remote(directory: str) -> Tuple[bool, str]:
    # Check if the directory is a remote Git repository (contains both https:// and .git)
    if "https://" in directory:
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
        
        # Return the path to the cloned repository
        return True, repo_path
    else:
        # If it's not a remote repository, return the directory path as is
        return False, directory
