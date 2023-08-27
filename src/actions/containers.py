from typing import Generator, List

import docker
from src.actions.__interface import table_command


def generator() -> Generator[List[str], None, None]:
    for contenedor in docker.from_env().containers.list():
        servicio = contenedor.image.tags[0]
        creado = contenedor.attrs['Created']
        puertos = contenedor.attrs['HostConfig']['PortBindings']
        nombre = contenedor.name

        yield [servicio, creado, puertos, nombre]


def containers(stream: bool = True):
    table_command(f=generator, headers=['SERVICE', 'CREATED', 'PORTS', 'NAMES'], stream=stream)