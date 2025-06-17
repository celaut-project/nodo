import os
import subprocess
from typing import List, Optional

from protos import celaut_pb2 as celaut
from src.gateway.utils import generate_node_peer_info
from src.utils import logger as log
from src.utils.env import DOCKER_COMMAND, DOCKER_NETWORK, EnvManager

from src.utils.env import EnvManager

env_manager = EnvManager()

CACHE = env_manager.get_env("CACHE")

def get_config(config: Optional[celaut.Configuration], resources: celaut.Sysresources,  network_resolution: List[celaut.ConfigurationFile.NetworkResolution]=[]) -> celaut.ConfigurationFile:

    __config__ = celaut.ConfigurationFile()

    __config__.gateway.CopyFrom(generate_node_peer_info(network=DOCKER_NETWORK).instance)
    
    if config: 
        __config__.CopyFrom(config)
    
    if network_resolution:
        __config__.network_resolution.extend(network_resolution)

    if resources: 
        __config__.initial_sysresources.CopyFrom(resources)

    return __config__

def write_config(path: str, config: celaut.ConfigurationFile):
    with open(f'{path}/__config__', 'wb') as file:
        file.write(config.SerializeToString())

def set_config(container_id: str, 
               config: Optional[celaut.Configuration], 
               resources: celaut.Sysresources,
               api: celaut.Service.Container.Config,
               network_resolution: List[celaut.ConfigurationFile.NetworkResolution]
            ):
    
    __config__ = get_config(config=config, resources=resources, network_resolution=network_resolution)
    log.LOGGER(f"Configuration file for the container {container_id}: \n {__config__}")

    os.mkdir(CACHE + container_id)

    # TODO: Check if api.format is valid or make the serializer for it.

    write_config(path=CACHE + container_id, config=__config__)
    
    while 1:
        try:
            subprocess.run(
                f"{DOCKER_COMMAND} cp {CACHE}{container_id}/__config__ {container_id}:/{'/'.join(api.path)}",
                shell=True
            )
            break
        except subprocess.CalledProcessError as e:
            log.LOGGER(e.output)

    # TODO auxiliar commented.
    # os.remove(CACHE + container_id + '/__config__')
    # os.rmdir(CACHE + container_id)
