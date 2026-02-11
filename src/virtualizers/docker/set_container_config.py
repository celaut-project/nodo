import os
import subprocess
from typing import List, Optional

from protos import celaut_pb2 as celaut
from src.gateway.utils import generate_node_peer_info
from src.utils import logger as log
from src.utils.config import DOCKER_COMMAND, ConfigManager

from src.utils.config import ConfigManager

env_manager = ConfigManager()

CACHE = env_manager.get("CACHE")
DOCKER_NETWORK = env_manager.get("DOCKER_NETWORK")

def get_config(config: Optional[celaut.Configuration], resources: celaut.Sysresources,  network_resolution: List[celaut.ConfigurationFile.NetworkResolution]=[]) -> celaut.ConfigurationFile:

    __config__ = celaut.ConfigurationFile()

    local_peer = generate_node_peer_info(network=DOCKER_NETWORK)
    log.LOGGER(f"Local peer generated: \n {local_peer}")
    __config__.gateway.CopyFrom(local_peer.instance)

    if config: 
        __config__.config.CopyFrom(config)
        log.LOGGER(f"Configuration loaded: \n {__config__.config}")
    
    if network_resolution:
        __config__.network_resolution.extend(network_resolution)
        log.LOGGER(f"Network resolution loaded: \n {__config__.network_resolution}")

    if resources: 
        __config__.initial_sysresources.CopyFrom(resources)
        log.LOGGER(f"Initial system resources loaded: \n {__config__.initial_sysresources}")

    log.LOGGER(f"Configuration file generated")
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
    log.LOGGER(f"Configuration file for the container {container_id}")

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
