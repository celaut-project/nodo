"""
Helpers to build and write a service `__config__` (celaut.ConfigurationFile).

These used to live in src/virtualizers/docker/set_container_config.py. They are
not Docker-specific (the Docker-specific `set_config`, which `docker cp`-ed the
file into a container, stayed with the now-removed Docker virtualizer). They are
used by the `ggconf` dev command, so they live in a neutral util module.
"""
from typing import List, Optional

from protos import celaut_pb2 as celaut
from src.gateway.utils import generate_node_peer_info
from src.utils import logger as log
from src.utils.config import ConfigManager

env_manager = ConfigManager()

# Network interface advertised as the local gateway in the generated config.
# Defaults to the Cloud Hypervisor bridge.
GATEWAY_NETWORK = env_manager.get("virtualizers.ch.NETWORK_BRIDGE_NAME", "br-ch")


def get_config(
    config: Optional[celaut.Configuration],
    resources: celaut.Sysresources,
    network_resolution: List[celaut.ConfigurationFile.NetworkResolution] = [],
) -> celaut.ConfigurationFile:
    __config__ = celaut.ConfigurationFile()

    local_peer = generate_node_peer_info(network=GATEWAY_NETWORK)
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

    log.LOGGER("Configuration file generated")
    return __config__


def write_config(path: str, config: celaut.ConfigurationFile):
    with open(f"{path}/__config__", "wb") as file:
        file.write(config.SerializeToString())
