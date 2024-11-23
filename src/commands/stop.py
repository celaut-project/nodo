import os
from src.manager.manager import prune_container


def stop(instance: str):
    # Check if script is run as root
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return
    
    if prune_container(token=instance):
        print(f"Service instance {instance} deleted.")
    else:
        print(f"Something was wrong.")
