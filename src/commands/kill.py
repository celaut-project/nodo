import os
from src.manager.manager import stop_instance


def kill(instance: str):
    # Check if script is run as root
    if os.geteuid() != 0:
        print("This script requires superuser privileges. Please run with sudo.")
        return
    
    if stop_instance(token=instance) is not None:
        print(f"Service instance {instance} deleted.")
    else:
        print(f"Something was wrong.")
