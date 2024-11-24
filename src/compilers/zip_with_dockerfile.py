import codecs
from typing import Generator, List, Tuple, Union

from protos.celaut_pb2 import Any

from src.utils import logger as log
import json
import os, subprocess
import src.manager.resources_manager as resources_manager
from grpcbigbuffer import client as grpcbb
from grpcbigbuffer import buffer_pb2, block_builder
from protos import celaut_pb2 as celaut, compile_pb2, gateway_pb2_grpcbf
from src.utils.env import EnvManager, SHA3_256_ID, DOCKER_COMMAND, COMPILER_SUPPORTED_ARCHITECTURES
from src.utils.utils import get_service_hex_main_hash
from src.utils.verify import get_service_list_of_hashes, calculate_hashes, calculate_hashes_by_stream
from src.utils.env import EnvManager

env_manager = EnvManager()

CACHE = env_manager.get_env("CACHE")
COMPILER_MEMORY_SIZE_FACTOR = env_manager.get_env("COMPILER_MEMORY_SIZE_FACTOR")
SAVE_ALL = env_manager.get_env("SAVE_ALL")
MIN_BUFFER_BLOCK_SIZE = env_manager.get_env("MIN_BUFFER_BLOCK_SIZE")


class Compiler:
    def __init__(self, path, aux_id):
        super().__init__()
        self.blocks: List[bytes] = []
        self.service = compile_pb2.Service()
        self.metadata = celaut.Any.Metadata()
        self.path = path
        self.json = json.load(open(self.path + "service.json", "r"))
        self.aux_id = aux_id
        self.error_msg = None

        arch = None
        for a in COMPILER_SUPPORTED_ARCHITECTURES:
            if self.json.get('architecture') in a: arch = a[0]

        if not arch: raise Exception("Can't compile this service, not supported architecture.")

        # Directories are created on cache.
        os.system("mkdir " + CACHE + self.aux_id + "/building")
        os.system("mkdir " + CACHE + self.aux_id + "/filesystem")

        # Log the selected architecture.
        log.LOGGER(f"Arch selected {arch}")

        # Build container and get compressed layers.
        if not os.path.isfile(self.path + 'Dockerfile'):
            raise Exception("Error: Dockerfile not found.")

        # Define Docker commands
        commands = [
            f"{DOCKER_COMMAND} buildx build --platform {arch} --no-cache -t builder{self.aux_id} {self.path}",
            f"{DOCKER_COMMAND} save builder{self.aux_id} > {CACHE}{self.aux_id}/building/container.tar",
            f"tar -xvf {CACHE}{self.aux_id}/building/container.tar -C {CACHE}{self.aux_id}/building/"
        ]

        # Execute Docker commands with error handling using subprocess
        for cmd in commands:
            try:
                # Run command and capture output
                process = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                stdout, stderr = process.communicate()
                
                if process.returncode != 0:
                    # If command failed, store the error message and log it
                    self.error_msg = stderr.strip() or stdout.strip() or f"Command failed with return code {process.returncode}"
                    log.LOGGER(f"Error executing command: {cmd}")
                    log.LOGGER(f"Error message: {self.error_msg}")
                    return  # Exit initialization if there's an error
                
            except Exception as e:
                # Handle any other exceptions that might occur
                self.error_msg = str(e)
                log.LOGGER(f"Exception while executing command: {cmd}")
                log.LOGGER(f"Exception: {self.error_msg}")
                return

        try:
            # Get the buffer length only if previous commands succeeded
            size_cmd = f"{DOCKER_COMMAND} image inspect builder{self.aux_id} --format='{{{{.Size}}}}'"
            process = subprocess.run(size_cmd, shell=True, capture_output=True, text=True)
            if process.returncode == 0:
                self.buffer_len = int(process.stdout.strip())
            else:
                self.error_msg = f"Failed to get image size: {process.stderr.strip()}"
                log.LOGGER(self.error_msg)
                return
                
        except Exception as e:
            self.error_msg = f"Error getting image size: {str(e)}"
            log.LOGGER(self.error_msg)
            return
        
        # Check first tag for use as name
        self.tag = self.json["tag"] if "tag" in self.json else None

def ok(path, aux_id) -> Tuple[str, celaut.Any.Metadata, Union[str, compile_pb2.Service]]:
    spec_file = Compiler(path=path, aux_id=aux_id)
    
    # Check if there was an error during initialization
    if spec_file.error_msg:
        return None, None, spec_file.error_msg

    with resources_manager.mem_manager(len=COMPILER_MEMORY_SIZE_FACTOR * spec_file.buffer_len):
        spec_file.parseContainer()
        spec_file.parseApi()
        spec_file.parseNetwork()

        identifier, metadata, service = spec_file.save()

    os.system(DOCKER_COMMAND+' tag builder' + aux_id + ' ' + identifier + '.docker')
    os.system(DOCKER_COMMAND + ' rmi builder' + aux_id)
    os.system('rm -rf ' + CACHE + aux_id + '/')
    return identifier, metadata, service


def zipfile_ok(zip: str) -> Tuple[str, celaut.Any.Metadata, Union[str, compile_pb2.Service]]:
    import random
    aux_id = str(random.random())
    os.system('mkdir ' + CACHE + aux_id)
    os.system('mkdir ' + CACHE + aux_id + '/for_build')
    os.system('unzip ' + zip + ' -d ' + CACHE + aux_id + '/for_build')
    os.system('rm ' + zip)
    return ok(
        path=CACHE + aux_id + '/for_build/',
        aux_id=aux_id
    )  # Specification file


def compile_zip(zip: str, saveit: bool = SAVE_ALL) -> Generator[buffer_pb2.Buffer, None, None]:
    log.LOGGER('Compiling zip ' + str(zip))
    service_id, metadata, service = zipfile_ok(zip=zip)
    
    if not service_id and not metadata and service:
        error_msg = service
        yield from grpcbb.serialize_to_buffer(
            message_iterator=[
                compile_pb2.CompileOutputError(
                    message=error_msg
                )
            ],
            indices=gateway_pb2_grpcbf.CompileOutput_indices
        )
        
    else:
        yield from grpcbb.serialize_to_buffer(
                message_iterator=[
                    compile_pb2.CompileOutputServiceId(
                        id=bytes.fromhex(service_id)
                    ),
                    metadata,
                    grpcbb.Dir(dir=service, _type=compile_pb2.Service)
                    if type(service) is str else service
                ],
                indices=gateway_pb2_grpcbf.CompileOutput_indices
        )

    # shutil.rmtree(service_with_meta.name)
    # TODO if saveit: convert dirs to local partition model and save it into the registry.
