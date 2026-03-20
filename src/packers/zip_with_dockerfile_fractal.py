import codecs
from typing import Generator, List, Tuple, Union

from src.utils import logger as log
import json
import os, subprocess
import src.manager.resources as resources
from bee_rpc import client as grpcbb
from bee_rpc import buffer_pb2, block_builder
from bee_rpc.reader import read_from_registry
from protos import celaut_pb2 as celaut, pack_pb2, gateway_bee
from src.utils.config import ConfigManager, SHA3_256_ID, DOCKER_COMMAND, DOCKER_ENV, PACKER_SUPPORTED_ARCHITECTURES
from src.utils.utils import get_service_hex_main_hash
from src.utils.verify import get_service_list_of_hashes, calculate_hashes, calculate_hashes_by_stream
from src.utils.config import ConfigManager

env_manager = ConfigManager()

CACHE = env_manager.get("CACHE")
PACKER_MEMORY_SIZE_FACTOR = env_manager.get("PACKER_MEMORY_SIZE_FACTOR")
SAVE_ALL = env_manager.get("SAVE_ALL")
MIN_BUFFER_BLOCK_SIZE = env_manager.get("MIN_BUFFER_BLOCK_SIZE")


class ZipContainerPacker:
    def __init__(self, path, aux_id):
        self.blocks: List[bytes] = []
        self.service = celaut.Service()
        self.metadata = celaut.Metadata()
        self.path = path
        self.json = json.load(open(self.path + "service.json", "r"))
        self.aux_id = aux_id
        self.error_msg = None
        self._validate_service_json_shape()

        arch = None
        for a in PACKER_SUPPORTED_ARCHITECTURES:
            if self.json.get('architecture') in a: arch = a[0]

        if not arch: raise Exception("Can't pack this service, not supported architecture.")

        # Directories are created on cache.
        os.makedirs(os.path.join(CACHE, self.aux_id, "building"), exist_ok=True)
        os.makedirs(os.path.join(CACHE, self.aux_id, "filesystem"), exist_ok=True)

        # Log the selected architecture.
        log.LOGGER(f"Arch selected {arch}")

        # Build container and get compressed layers.
        if not os.path.isfile(self.path + 'Dockerfile'):
            raise Exception("Error: Dockerfile not found.")

        build_cmd = DOCKER_COMMAND + [
            "buildx", "build",
            "--platform", arch,
            "--no-cache",
            "-t", f"builder{self.aux_id}",
            self.path,
        ]
        save_cmd = DOCKER_COMMAND + ["save", f"builder{self.aux_id}"]
        tar_cmd = [
            "tar",
            "-xvf",
            f"{CACHE}{self.aux_id}/building/container.tar",
            "-C",
            f"{CACHE}{self.aux_id}/building/",
        ]

        def _run(cmd, env=None, stdout=None, text=True):
            result = subprocess.run(
                cmd,
                env=env,
                stdout=stdout,
                stderr=subprocess.PIPE,
                text=text
            )
            if result.returncode != 0:
                err = result.stderr.strip() if text else result.stderr.decode("utf-8", errors="replace").strip()
                self.error_msg = err or f"Command failed with return code {result.returncode}"
                log.LOGGER(f"Error executing command: {cmd}")
                log.LOGGER(f"Error message: {self.error_msg}")
                return False
            return True

        # Execute Docker build
        log.LOGGER(" ".join(build_cmd))
        if not _run(build_cmd, env=DOCKER_ENV):
            return

        # Save image to tar
        log.LOGGER(" ".join(save_cmd))
        try:
            with open(f"{CACHE}{self.aux_id}/building/container.tar", "wb") as tar_file:
                if not _run(save_cmd, env=DOCKER_ENV, stdout=tar_file, text=False):
                    return
        except Exception as e:
            self.error_msg = str(e)
            log.LOGGER(f"Exception while executing command: {save_cmd}")
            log.LOGGER(f"Exception: {self.error_msg}")
            return

        # Extract tar
        log.LOGGER(" ".join(tar_cmd))
        if not _run(tar_cmd):
            return

        try:
            # Get the buffer length only if previous commands succeeded
            size_cmd = DOCKER_COMMAND + [
                "image", "inspect",
                f"builder{self.aux_id}",
                "--format", "{{.Size}}",
            ]
            process = subprocess.run(size_cmd, env=DOCKER_ENV, capture_output=True, text=True)
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

    def _validate_service_json_shape(self) -> None:
        resources = self.json.get("resources", {})
        
    def parseContainer(self):
        def _normalize_path_segments(raw_path):
            if isinstance(raw_path, str):
                raw_items = [raw_path]
            else:
                raw_items = [str(item) for item in raw_path]

            normalized = []
            for item in raw_items:
                for segment in item.split("/"):
                    clean = segment.strip()
                    if clean:
                        normalized.append(clean)
            return normalized

        def parseFilesys() -> None:
            # Save his filesystem on cache.
            for layer in os.listdir(CACHE + self.aux_id + "/building/"):
                if os.path.isdir(CACHE + self.aux_id + "/building/" + layer):
                    log.LOGGER('Unzipping layer ' + layer)
                    os.system(
                        "tar -xvf " + CACHE + self.aux_id + "/building/" + layer + "/layer.tar -C "
                        + CACHE + self.aux_id + "/filesystem/"
                    )
            # Add filesystem data to filesystem buffer object.
            def recursive_parsing(directory: str) -> celaut.Service.Container.Filesystem:
                host_dir = CACHE + self.aux_id + "/filesystem"
                filesystem = celaut.Service.Container.Filesystem()
                for b_name in os.listdir(host_dir + directory):
                    if b_name == '.wh..wh..opq':
                        # https://github.com/opencontainers/image-spec/blob/master/layer.md#opaque-whiteout
                        continue
                    branch = celaut.Service.Container.Filesystem.ItemBranch()
                    branch.name = os.path.basename(b_name)
                    # It's a link.
                    if os.path.islink(host_dir + directory + b_name):
                        branch.link.dst = directory + b_name
                        branch.link.src = os.path.realpath(host_dir + directory + b_name)[
                                          len(host_dir):] if host_dir in os.path.realpath(
                            host_dir + directory + b_name) else os.path.realpath(host_dir + directory + b_name)
                    # It's a file.
                    elif os.path.isfile(host_dir + directory + b_name):
                        if os.path.getsize(host_dir + directory + b_name) < MIN_BUFFER_BLOCK_SIZE:
                            with open(host_dir + directory + b_name, 'rb') as file:
                                branch.file = file.read()
                        else:
                            block_hash, block = block_builder.create_block(
                                file_path=host_dir + directory + b_name,
                                copy=True
                            )
                            branch.file = block.SerializeToString()
                            if block_hash not in self.blocks:
                                self.blocks.append(block_hash)
                    # It's a folder.
                    elif os.path.isdir(host_dir + directory + b_name):
                        branch.filesystem.CopyFrom(
                            recursive_parsing(directory=directory + b_name + '/')
                        )
                    filesystem.branch.append(branch)
                return filesystem

            filesystem = recursive_parsing(directory="/")
            
            # Create a filesystem directory with a structure with all blocks of the filesystem.
            _, fs_blocked_filename = block_builder.build_multiblock(pf_object_with_block_pointers=filesystem, blocks=self.blocks)
            
            # Save the filesystem binary.
            fs_bin_filename = CACHE + self.aux_id + '/filesystem_binary.bin'
            with open(fs_bin_filename, "wb+") as file:
                for chunk in read_from_registry(filename=fs_blocked_filename):
                    file.write(chunk.chunk)

            os.system(fs_blocked_filename)
        
            block_hash, block = block_builder.create_block(
                file_path=fs_bin_filename,
                copy=False  # We don't copy the file, it's temporary and would be deleted.
            )

            if block_hash not in self.blocks:
                self.blocks.append(block_hash)
            
            self.service.container.filesystem = block.SerializeToString()

    
        res = self.json.get('resources', {})

        # 0 is considered as no limit.

        # Extract at_init and at_most resource configurations
        at_init = res.get("at_init", {})
        at_most = res.get("at_most", {})

        # Extract initial values with defaults
        init_blkio_weight = int(at_init.get("blkio_weight", 0))
        init_cpu_period   = int(at_init.get("cpu_period", 0))
        init_cpu_quota    = int(at_init.get("cpu_quota", 0))
        init_mem_limit    = int(at_init.get("mem_limit", 10_000_000))       # 10MB by default
        init_disk_space   = int(at_init.get("disk_space", 2_000_000_000))   # 2GB by default

        # Ensure at_most values are at least as high as at_init
        most_blkio_weight = max(init_blkio_weight, int(at_most.get("blkio_weight", 0)))
        most_cpu_period   = max(init_cpu_period, int(at_most.get("cpu_period", 0)))
        most_cpu_quota    = max(init_cpu_quota, int(at_most.get("cpu_quota", 0)))
        most_mem_limit    = max(init_mem_limit, int(at_most.get("mem_limit", 10_000_000)))       # 10MB by default
        most_disk_space   = max(init_disk_space, int(at_most.get("disk_space", 2_000_000_000)))   # 2GB by default

        # Assign values to the container resources
        r = self.service.container.resources
        r.at_init.blkio_weight = init_blkio_weight
        r.at_init.cpu_period = init_cpu_period
        r.at_init.cpu_quota = init_cpu_quota
        r.at_init.mem_limit = init_mem_limit
        r.at_init.disk_space = init_disk_space

        r.at_most.blkio_weight = most_blkio_weight
        r.at_most.cpu_period = most_cpu_period
        r.at_most.cpu_quota = most_cpu_quota
        r.at_most.mem_limit = most_mem_limit
        r.at_most.disk_space = most_disk_space


        # Entrypoint
        init = self.json.get("init", {})
        if not isinstance(init, dict):
            init = {}

        entry_path = _normalize_path_segments(init.get("entry_path", []))
        if not entry_path and self.json.get("entrypoint"):
            # Legacy compatibility: map service.json entrypoint -> container.init.entry_path
            entry_path = _normalize_path_segments(self.json.get("entrypoint"))
        self.service.container.init.entry_path.extend(entry_path)
        for key, value in init.get("xattrs", {}).items():
            if isinstance(value, str):
                self.service.container.init.xattrs[key] = value.encode("utf-8")
            else:
                self.service.container.init.xattrs[key] = bytes(value)
        
        # Arch
        
        # Config file spec.
        config_declaration = self.json.get("config_declaration", {"path": ["__config__"]})
        config_path = _normalize_path_segments(config_declaration.get("path", ["__config__"]))
        self.service.container.config_declaration.path.extend(config_path)
        self.service.container.config_declaration.format.CopyFrom(
            celaut.DataFormat()
        )
        self.service.container.architecture.tags.extend([self.json.get('architecture')])
        
        # Expected Node gateway interface
        # pass

        # Filesystem
        parseFilesys()
    
    def parseApi(self):
        
        # Envs
        if self.json.get('envs'):
            for env in self.json.get('envs'):
                try:
                    with open(self.path + env + ".field", "rb") as env_desc:
                        self.service.api.environment_variables[env].ParseFromString(env_desc.read())
                except FileNotFoundError:
                    pass

        if not self.json.get('api'): return
        
        for item in self.json.get('api'):  # iterate slots.
            slot = celaut.Service.Api.Slot()
            slot.port = item.get('port')
            slot.protocol_stack.append(
                celaut.Service.Api.Protocol(
                    tags=item.get('protocol')
                )
            )
            for method, gas_amount in item.get("gas_amount_per_call", {}).items():
                slot.gas_amount_per_call[method].n = str(gas_amount)
            self.service.api.slot.append(slot)
            
    def parseNetwork(self):
        if self.json.get('network'):
            for json_network in self.json.get("network", []):
                network = celaut.Service.Network()
                network.tags.extend(json_network['tags'])
                network.prose = json_network['prose']
                self.service.network.append(network)

    def save(self) -> Tuple[str, celaut.Metadata, Union[str, celaut.Service]]:

        service: Union[str, celaut.Service]

        # Generate the hashes.
        bytes_id, service_directory = block_builder.build_multiblock_fractal(
            pf_object_with_block_pointers=self.service,
            blocks=self.blocks
        )
        service_id: str = codecs.encode(bytes_id, 'hex').decode('utf-8')
        self.metadata.hashtag.hash.extend(
            [celaut.Metadata.HashTag.Hash(
                type=SHA3_256_ID,
                value=bytes_id
            )]
        )
            
        service = service_directory

        # Add the tag attribute as the first tag or tag list in the metadata. This could be used as the name of the service for better human identification.
        if self.tag and type(self.tag) is str: 
            self.metadata.hashtag.tag.extend([self.tag])
        elif self.tag and type(self.tag) is list: 
            self.metadata.hashtag.tag.extend(self.tag)

        # Metadata integrity validation.
        metadata_integrity_validation = [hash.type for hash in self.metadata.hashtag.hash]
        if len(metadata_integrity_validation) != len(set(metadata_integrity_validation)):
            _msg = "Metadata integrity validation exception.\n"
            for hash in list(self.metadata.hashtag.hash):
                _msg += f"-  {hash.type.hex()}: {hash.value.hex()}\n"
            log.LOGGER(_msg)
            raise Exception(_msg)
            
        return service_id, self.metadata, service

def ok(path, aux_id) -> Tuple[str, celaut.Metadata, Union[str, celaut.Service]]:
    spec_file = ZipContainerPacker(path=path, aux_id=aux_id)
    
    # Check if there was an error during initialization
    if spec_file.error_msg:
        return None, None, spec_file.error_msg

    
    _memory = int(PACKER_MEMORY_SIZE_FACTOR) * spec_file.buffer_len
    log.LOGGER(f"Try to lock {_memory / (1024**2):.2f} MB")
    with resources.mem_manager(len=_memory):
        spec_file.parseContainer()
        spec_file.parseApi()
        spec_file.parseNetwork()

        identifier, metadata, service = spec_file.save()

    # os.system(DOCKER_COMMAND+' tag builder' + aux_id + ' ' + identifier + '.docker')  <-- This avoids rebuilding the container on the first run, but it causes file permission issues since it inherits them as they were on the host. Preferably, if using Docker, it is better to rebuild it.
    try:
        subprocess.run(
            DOCKER_COMMAND + ["rmi", "-f", f"builder{aux_id}"],
            env=DOCKER_ENV,
            check=False
        )
    except Exception:
        pass
    os.system('rm -rf ' + CACHE + aux_id + '/')
    return identifier, metadata, service


def zipfile_ok(zip: str) -> Tuple[str, celaut.Metadata, Union[str, celaut.Service]]:
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


def pack_zip(zip: str, saveit: bool = SAVE_ALL) -> Generator[buffer_pb2.Buffer, None, None]:
    log.LOGGER('Compiling zip ' + str(zip))
    service_id, metadata, service = zipfile_ok(zip=zip)
    
    if not service_id and not metadata and service:
        error_msg = service
        yield from grpcbb.serialize_to_buffer(
            message_iterator=[
                pack_pb2.PackOutputError(
                    message=error_msg
                )
            ],
            indices=gateway_bee.PackOutput_indices
        )
        
    else:
        yield from grpcbb.serialize_to_buffer(
                message_iterator=[
                    pack_pb2.PackOutputServiceId(
                        id=bytes.fromhex(service_id)
                    ),
                    metadata,
                    grpcbb.Dir(dir=service, _type=celaut.Service)
                    if type(service) is str else service
                ],
                indices=gateway_bee.PackOutput_indices
        )

    # shutil.rmtree(service_with_meta.name)
    # TODO if saveit: convert dirs to local partition model and save it into the registry.
