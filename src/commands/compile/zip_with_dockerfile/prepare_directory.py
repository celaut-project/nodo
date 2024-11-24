from typing import Tuple
import os
import subprocess
import uuid

from src.utils.env import EnvManager

env_manager = EnvManager()

CACHE = env_manager.get_env("CACHE")

def prepare_directory(directory: str) -> Tuple[bool, str]:
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
    
    elif os.path.isdir(directory):
        # Remove the last character '/' from the path if it exists
        if directory[-1] == '/':
            directory = directory[:-1]
        
        repo_name = directory.split("/")[-1]
        
        # Generate a unique identifier (UUID) to avoid conflicts
        unique_id = str(uuid.uuid4())
        
        # Combine the repository name with the UUID to create a unique path
        repo_name_with_uuid = f"{repo_name}_{unique_id}"
        repo_path = os.path.join(CACHE, "git_repositories", repo_name_with_uuid)

        # Create the destination directory
        os.makedirs(repo_path, exist_ok=True)

        # Use subprocess.run for safer directory copying
        try:
            # Copy regular files and directories
            subprocess.run(["cp", "-r", directory, repo_path], check=True)
            
            # Copy hidden files and directories if they exist
            hidden_files = subprocess.run(["ls", "-A", directory], 
                                        capture_output=True, 
                                        text=True).stdout.split()
            
            for item in hidden_files:
                if item.startswith('.'):
                    source = os.path.join(directory, item)
                    subprocess.run(["cp", "-r", source, repo_path], check=True)
                    
            return False, repo_path
            
        except subprocess.CalledProcessError as e:
            raise Exception(f"Failed to copy directory: {str(e)}")
        
    else:
        raise Exception("SOMETHING SHOULD BE WRITTEN HERE.")
