import os
import subprocess
from typing import Optional

from protos import celaut_pb2 as celaut
from src.gateway.utils import generate_gateway_instance
from src.utils import logger as l
from src.utils.env import CACHE, DOCKER_COMMAND, DOCKER_NETWORK


def set_config(container_id: str, config: Optional[celaut.Configuration], resources: celaut.Sysresources,
               api: celaut.Service.Container.Config):
    __config__ = celaut.ConfigurationFile()
    __config__.gateway.CopyFrom(generate_gateway_instance(network=DOCKER_NETWORK).instance)
    if config: __config__.config.CopyFrom(config)
    if resources: __config__.initial_sysresources.CopyFrom(resources)

    os.mkdir(CACHE + container_id)
    # TODO: Check if api.format is valid or make the serializer for it.

    with open(CACHE + container_id + '/__config__', 'wb') as file:
        file.write(__config__.SerializeToString())
    while 1:
        try:
            subprocess.run(
                DOCKER_COMMAND + ' cp ' + CACHE + container_id + '/__config__ ' + container_id + ':/' + '/'.join(
                    api.path),
                shell=True
            )
            break
        except subprocess.CalledProcessError as e:
            l.LOGGER(e.output)
    os.remove(CACHE + container_id + '/__config__')
    os.rmdir(CACHE + container_id)
