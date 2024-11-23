import os
from src.manager.manager import prune_container
from src.utils.env import SHA3_256_ID, EnvManager

env_manager = EnvManager()

SHA3_256 = SHA3_256_ID.hex()



def stop(instance: str):
    # Check if script is run as root
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return
    
    if prune_container(token=instance):
        print(f"Service instance {instance} deleted.")
    else:
        print(f"Something was wrong.")
