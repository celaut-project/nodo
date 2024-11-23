

from src.manager.manager import prune_container
from src.utils.env import SHA3_256_ID, EnvManager

env_manager = EnvManager()

SHA3_256 = SHA3_256_ID.hex()



def stop(instance: str):
    if prune_container(token=instance):
        print(f"Service instance {instance} deleted.")
    else:
        print(f"Something was wrong.")
