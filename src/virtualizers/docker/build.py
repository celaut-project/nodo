
import json
import os
from pathlib import Path

import src.manager.resources as resources
import threading
from shutil import rmtree
from subprocess import check_output, CalledProcessError
from time import sleep, time
from typing import Tuple, Optional, Set

from bee_rpc.client import copy_block_if_exists

import src.utils.logger as l
from protos import celaut_pb2, celaut_pb2
from src.utils.runtime import DOCKER_COMMAND, DOCKER_ENV
from src.utils.config import ConfigManager
from src.utils.arch_guard import ensure_native_arch
from src.utils.verify import get_service_hex_main_hash
from src.virtualizers.architecture import get_arch_tag

from src.utils.runtime import DOCKER_CLIENT

env_manager = ConfigManager()

BUILD_CONTAINER_MEMORY_SIZE_FACTOR = env_manager.get("BUILD_CONTAINER_MEMORY_SIZE_FACTOR")
WAIT_FOR_CONTAINER = int(env_manager.get("WAIT_FOR_CONTAINER"))
BLOCKDIR = env_manager.get("BLOCKDIR")
CACHE = env_manager.get("CACHE")
REGISTRY = env_manager.get("REGISTRY")


class WaitBuildException(Exception):

    def __init__(self, *args):
        self.message = None

    def __str__(self):
        return "Getting the container, the process will've time"


actual_building_processes_lock = threading.Lock()
actual_building_processes: Set[str] = set()  # list of hexadecimal string sha256 value hashes.


def __build_container_from_definition(service: celaut_pb2.Service,
                                    metadata: celaut_pb2.Metadata,
                                    service_id: str):
    # Build the container from filesystem definition.
    def write_item(b: celaut_pb2.Service.Container.Filesystem.ItemBranch, dir_element: str, symlinks_element):
        if b.HasField('filesystem'):
            os.mkdir(dir_element + b.name)
            write_fs(fs_element=b.filesystem, dir_element=dir_element + b.name + '/', symlinks_element=symlinks_element)

        elif b.HasField('file'):
            if not copy_block_if_exists(
                    buffer=b.file,
                    directory=dir_element + b.name
            ):
                open(dir_element + b.name, 'wb').write(
                    b.file
                )

        else:
            symlinks_element.append(b.link)

    def write_fs(fs_element: celaut_pb2.Service.Container.Filesystem, dir_element: str, symlinks_element):
        for branch in fs_element.branch:
            write_item(
                b=branch,
                dir_element=dir_element,
                symlinks_element=symlinks_element
            )

    l.LOGGER('Build process of ' + service_id + ': wait for unlock the memory.')

    # Get the size of the service's biggest block.
    # with open(f"{REGISTRY}{get_service_hex_main_hash(metadata=metadata)}/_.json", 'r') as f:  # TODO verify: why was not using the service_id?
    if os.path.isdir(f"{REGISTRY}{service_id}"):
        with open(f"{REGISTRY}{service_id}/_.json", 'r') as f:
            try:
                biggest_block_size: int = max([os.path.getsize(BLOCKDIR + c[0])
                                            for c in json.load(f) if type(c) is Tuple])
            except ValueError:
                biggest_block_size: int = 0
    else:
        biggest_block_size: int = os.path.getsize(f"{REGISTRY}{service_id}")

    with resources.mem_manager(
            len=sum([
                service.ByteSize(),
                biggest_block_size
            ]) * BUILD_CONTAINER_MEMORY_SIZE_FACTOR
    ):
        # TODO si el coste es mayor a la cantidad total se quedará esperando indefinidamente.
        l.LOGGER('Build process of ' + service_id + ': go to load all the buffer.')
        l.LOGGER('Build process of ' + service_id + ': filesystem load in memory.')

        # Take filesystem.
        fs = celaut_pb2.Service.Container.Filesystem()
        fs.ParseFromString(
            service.container.filesystem
        )

        # Take architecture.
        arch = get_arch_tag(service=service, metadata=metadata)
        ensure_native_arch(arch, context="docker build")
        # get_arch_tag, selecciona el tag de la arquitectura definida por el servicio,
        #  en base a la especificación y metadatos, que tiene el nodo para esa arquitectura.
        l.LOGGER('Build process of ' + service_id + ': select the architecture ' + str(arch))

        try:
            os.mkdir(CACHE)
        except:
            pass

        # Write all on cache.
        _dir = CACHE + 'builder' + service_id
        Path(_dir).mkdir(exist_ok=True, parents=True)
        fs_dir = _dir + '/fs'
        Path(fs_dir).mkdir(exist_ok=True, parents=True)
        symlinks = []
        l.LOGGER('Build process of ' + service_id + ': writting filesystem.')
        write_fs(fs_element=fs, dir_element=fs_dir + '/', symlinks_element=symlinks)

        # Build it.
        l.LOGGER('Build process of ' + service_id + ': building it ...')
        open(_dir + '/Dockerfile', 'w').write('FROM scratch\nCOPY --chmod=777 fs .')
        cache_id = service_id + str(time()) + '.cache'
        check_output(
            DOCKER_COMMAND + ["buildx", "build", "--platform", arch, "-t", cache_id, _dir + '/.'],
            env=DOCKER_ENV
        )
        l.LOGGER('Build process of ' + service_id + ': build it.')
        try:
            rmtree(_dir)
        except Exception:
            pass

        # Generate the symlinks.
        overlay_dir = check_output(
            DOCKER_COMMAND + ["inspect", "--format", "{{ .GraphDriver.Data.UpperDir }}", cache_id],
            env=DOCKER_ENV
        ).decode('utf-8').strip()
        l.LOGGER('Build process of ' + service_id + ': overlay dir ' + str(overlay_dir))
        for symlink in symlinks:
            try:
                if check_output('ln -s ' + symlink.src + ' ' + symlink.dst[1:], shell=True, cwd=overlay_dir)[
                   :2] == 'ln': break
            except CalledProcessError:
                l.LOGGER(
                    'Build process of ' + service_id + ': symlink error (CalledProcessError) ' + str(symlink.src) + str(
                        symlink.dst))
                break
            except AttributeError:
                l.LOGGER(
                    'Build process of ' + service_id + ': symlink error (AttributeError) ' + str(symlink.src) + str(
                        symlink.dst))

        check_output(
            DOCKER_COMMAND + ["image", "tag", cache_id, service_id + '.docker'],
            env=DOCKER_ENV
        )
        check_output(
            DOCKER_COMMAND + ["rmi", cache_id],
            env=DOCKER_ENV
        )
        l.LOGGER('Build process of ' + service_id + ': finished.')

        with actual_building_processes_lock:
            actual_building_processes.remove(service_id)


def build(
        service: celaut_pb2.Service,
        metadata: celaut_pb2.Metadata,
        service_id: Optional[str] = None,
) -> str:
    if not service_id:
        try:
            service_id = get_service_hex_main_hash(
                metadata=metadata
            )
        except Exception as e:
            l.LOGGER("Builder exception: can't obtain the service identifier")
            raise e

    l.LOGGER('Building ' + service_id)
    while True:
        try:
            # check if it's locally.
            check_output(
                DOCKER_COMMAND + ["inspect", service_id + '.docker'],
                env=DOCKER_ENV
            )
            return service_id

        except CalledProcessError:
            if service_id in actual_building_processes:
                sleep(WAIT_FOR_CONTAINER)
                continue

            with actual_building_processes_lock:
                actual_building_processes.add(service_id)

            __build_container_from_definition(
                service=service,
                metadata=metadata,
                service_id=service_id
            )


def is_service_built(service_hash: str) -> bool: 
    """Check if the service is built by comparing the service hash with existing Docker images."""
    try:
        # Get the list of images
        images = DOCKER_CLIENT().images.list()
        
        # Check if images exist and process tags safely
        for img in images:
            try:
                if img.tags and isinstance(img.tags[0], str):  # Validate tag structure
                    # Extract the hash from the tag and check if it matches the service_hash
                    if service_hash == img.tags[0].split('.')[0]:
                        return True
            except:
                continue
    except (IndexError, AttributeError) as e:
        # Log the error, handle exceptions for missing attributes or invalid indexing
        logger(f"An error occurred while checking if service is built: {e}")
    except Exception as e:
        logger(f"Unexpected error in __is_service_built: {e}")
    return False
