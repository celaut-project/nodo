from src.gateway.launcher.local_execution.dev_container import create_and_start_container
import os

def run_dev(path: str):
    # Check if script is run as root
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return

    print(f"Executing container for development purposes, project: {path}")
    create_and_start_container(service_path=path)