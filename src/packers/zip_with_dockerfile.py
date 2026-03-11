import base64
import codecs
import fcntl
from typing import Generator, List, Tuple

from src.utils import logger as log
import json
import os, subprocess, platform, shlex, sys, uuid
import src.manager.resources as resources
from bee_rpc import client as grpcbb
from bee_rpc.utils import modify_env
from bee_rpc import buffer_pb2, block_builder
from protos import celaut_pb2 as celaut, pack_pb2, gateway_bee
from src.utils.config import ConfigManager, SHA3_256_ID, DOCKER_COMMAND, PACKER_SUPPORTED_ARCHITECTURES
from src.utils.verify import calculate_hashes, calculate_hashes_by_stream
from src.utils.config import ConfigManager
from src.manager.resources import IOBigData

env_manager = ConfigManager()

CACHE = env_manager.get("CACHE")
BLOCKDIR = env_manager.get("BLOCKDIR")
PACKER_MEMORY_SIZE_FACTOR = env_manager.get("PACKER_MEMORY_SIZE_FACTOR")
SAVE_ALL = env_manager.get("SAVE_ALL")
MIN_BUFFER_BLOCK_SIZE = env_manager.get("MIN_BUFFER_BLOCK_SIZE")

# Ensure bee_rpc uses the configured cache and block directories.
if CACHE:
    os.makedirs(CACHE, exist_ok=True)
if BLOCKDIR:
    os.makedirs(BLOCKDIR, exist_ok=True)
    modify_env(cache_dir=CACHE, block_dir=BLOCKDIR)


class ZipContainerPacker:
    def __init__(self, path, aux_id):
        self.blocks: List[bytes] = []
        self.service = pack_pb2.Service()
        self.metadata = celaut.Metadata()
        self.path = path
        self.json = json.load(open(self.path + "service.json", "r"))
        self.aux_id = aux_id
        self.error_msg = None

        arch = None
        for a in PACKER_SUPPORTED_ARCHITECTURES:
            if self.json.get('architecture') in a: arch = a[0]

        if not arch: raise Exception("Can't pack this service, not supported architecture.")

        # 1. Architecture detection
        host_arch = platform.machine().lower()
        target_arch = arch

        # 2. Prepare output path
        dest_path = os.path.join(CACHE, self.aux_id, "filesystem")
        os.makedirs(dest_path, exist_ok=True)
        tar_path = os.path.join(CACHE, self.aux_id, "filesystem.tar")

        # 3. Construct secure command
        build_cmd = shlex.split(DOCKER_COMMAND) + [
            "buildx", "build",
            "--platform", target_arch,
            "--progress", "plain",
            "--no-cache",
            "--output", f"type=tar,dest={tar_path}",
            self.path
        ]

        # 4. Stability Shield (If emulating ARM on Intel, limit threads)
        if "arm64" in target_arch and "x86" in host_arch:
            build_cmd.insert(-1, "--build-arg")
            build_cmd.insert(-1, "CARGO_BUILD_JOBS=1")  # For Rust code
            build_cmd.insert(-1, "--build-arg")
            build_cmd.insert(-1, "MAKEFLAGS=-j2")  # For C/C++ code

        # 5. Secure execution
        try:
            log.LOGGER(f"Starting build {target_arch} on host {host_arch}...")
            
            process = subprocess.Popen(
                build_cmd, 
                cwd=self.path, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            # TODO yield the logs and show on commands/packer
            full_output = []
            for line in iter(process.stdout.readline, ""):
                line_str = line.strip()
                if line_str:
                    log.LOGGER(line_str)
                    full_output.append(line_str)
            
            process.wait()
            if process.returncode != 0:
                self.error_msg = f"Critical build error: Command {build_cmd} returned non-zero exit status {process.returncode}.\n"
                self.error_msg += "\n".join(full_output)
                log.LOGGER(self.error_msg)
                return

            log.LOGGER(f"Extracting {tar_path} to {dest_path}...")
            import tarfile
            with tarfile.open(tar_path) as tar:
                tar.extractall(path=dest_path)
            os.remove(tar_path)

            log.LOGGER("Filesystem export completed successfully.")
            
            # Calculate buffer length from the exported files
            total_size = 0
            for dirpath, _, filenames in os.walk(dest_path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        total_size += os.path.getsize(fp)
            self.buffer_len = total_size

        except Exception as e:
            self.error_msg = f"Unexpected error during build: {str(e)}"
            log.LOGGER(self.error_msg)
            return

        # Check first tag for use as name
        self.tag = self.json.get("tag")
        
    def parseContainer(self):
        def parseFilesys() -> celaut.Metadata.HashTag:
            # File system is already exported to filesystem/ by buildx
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
            self.service.container.filesystem.CopyFrom(recursive_parsing(directory="/"))

            return celaut.Metadata.HashTag(
                hash=calculate_hashes(
                    value=self.service.container.filesystem.SerializeToString()
                ) if not self.blocks else
                calculate_hashes_by_stream(
                    value=grpcbb.read_multiblock_directory(
                        directory=block_builder.build_multiblock(
                            pf_object_with_block_pointers=self.service.container.filesystem,
                            blocks=self.blocks
                        )[1],
                        delete_directory=True,
                        ignore_blocks=True
                    )
                )
            )
    
        res = self.json.get('resources', {})

        # 0 is considered as no limit.

        # start_time_ms es opcional
        self.service.container.resources.start_time_ms = int(res.get("start_time_ms", 0))

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
        if self.json.get('entrypoint'):
            self.service.container.entrypoint.append(self.json.get('entrypoint'))
        
        # Arch
        
        # Config file spec.
        self.service.container.config.path.append('__config__')
        self.service.container.config.format.CopyFrom(
            celaut.DataFormat()  # celaut.ConfigFile definition.
        )
        self.service.container.architecture.tags.extend([self.json.get('architecture')])
        
        # Expected Gateway.
        
        # Add container metadata to the global metadata.
        self.metadata.hashtag.attr_hashtag.append(
            celaut.Metadata.HashTag.AttrHashTag(
                key=1,  # Container attr.
                value=[
                    celaut.Metadata.HashTag(
                        attr_hashtag=[
                            celaut.Metadata.HashTag.AttrHashTag(
                                key=2,  # Filesystem
                                value=[parseFilesys()]
                            )
                        ]
                    )
                ]
            )
        )
    
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
            self.service.api.slot.append(slot)
            
    def parseNetwork(self):
        if self.json.get('network'):
            for json_network in self.json.get("network", []):
                network = celaut.Service.Network()
                network.tags.extend(json_network['tags'])
                network.prose = json_network['prose']
                self.service.network.append(network)

    def save(self) -> Tuple[str, celaut.Metadata, str]:
        # Always build a multiblock directory so the service is returned as a path.
        bytes_id, service_directory = block_builder.build_multiblock(
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

        """  <!-- Validation don't needed here -->
        
            from hashlib import sha3_256
            validate_content = sha3_256()
            for i in grpcbb.read_multiblock_directory(directory=service_directory):
                validate_content.update(i)
            if validate_content.digest() != bytes_id:
                raise Exception(f"Invalid packing, wrong validated content {validate_content.hexdigest()}, but should be {bytes.hex(bytes_id)}")

        """
            
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

def ok(path, aux_id) -> Tuple[str, celaut.Metadata, str]:
    spec_file = ZipContainerPacker(path=path, aux_id=aux_id)
    
    # Check if there was an error during initialization
    if spec_file.error_msg:
        return None, None, spec_file.error_msg

    
    _memory = int(PACKER_MEMORY_SIZE_FACTOR) * spec_file.buffer_len
    log.LOGGER(f"Try to lock {_memory / (1024**2):.2f} MB of RAM for packing process (filesystem size: {spec_file.buffer_len / (1024**2):.2f} MB). RAM avaliable before locking: {IOBigData().get_ram_avaliable() / (1024**2):.2f} MB")
    with resources.mem_manager(len=_memory):
        log.LOGGER(f"RAM locked successfully for packing process. RAM avaliable after locking: {IOBigData().get_ram_avaliable() / (1024**2):.2f} MB")
        spec_file.parseContainer()
        spec_file.parseApi()
        spec_file.parseNetwork()

        identifier, metadata, service = spec_file.save()

    # os.system(DOCKER_COMMAND+' tag builder' + aux_id + ' ' + identifier + '.docker')  <-- This avoids rebuilding the container on the first run, but it causes file permission issues since it inherits them as they were on the host. Preferably, if using Docker, it is better to rebuild it.
    os.system('rm -rf ' + CACHE + aux_id + '/')
    return identifier, metadata, service


def zipfile_ok(zip: str) -> Tuple[str, celaut.Metadata, str]:
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
    lock_file = _acquire_pack_lock()
    try:
        result_path = os.path.join(CACHE, f"pack_result_{uuid.uuid4().hex}.json")
        main_dir = env_manager.get("MAIN_DIR") or os.getcwd()
        cmd = [
            sys.executable, "-m", "src.packers.zip_with_dockerfile",
            "--worker", zip, result_path
        ]
        proc = subprocess.run(cmd, cwd=main_dir)

        if proc.returncode != 0:
            yield from grpcbb.serialize_to_buffer(
                message_iterator=[
                    pack_pb2.PackOutputError(
                        message=f"Subprocess pack failed with exit code {proc.returncode}."
                    )
                ],
                indices=gateway_bee.PackOutput_indices
            )
            return

        if not os.path.exists(result_path):
            yield from grpcbb.serialize_to_buffer(
                message_iterator=[
                    pack_pb2.PackOutputError(
                        message=f"Subprocess pack did not produce result file: {result_path}"
                    )
                ],
                indices=gateway_bee.PackOutput_indices
            )
            return

        with open(result_path, "r") as f:
            result = json.load(f)
        os.remove(result_path)
        
        error_msg = result.get("error")
        if error_msg:
            service_id, metadata, service = None, None, error_msg
        else:
            service_id = result.get("service_id")
            metadata_b64 = result.get("metadata_b64")
            metadata = celaut.Metadata.FromString(base64.b64decode(metadata_b64)) if metadata_b64 else None
            service = result.get("service_dir")
            if service is None:
                service_id, metadata, service = None, None, "Worker did not return service directory."
    finally:
        _release_pack_lock(lock_file)
    
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
                    grpcbb.Dir(dir=service, _type=pack_pb2.Service)
                    if type(service) is str else service
                ],
                indices=gateway_bee.PackOutput_indices
        )

    # shutil.rmtree(service_with_meta.name)
    # TODO if saveit: convert dirs to local partition model and save it into the registry.


def _acquire_pack_lock():
    lock_path = os.path.join(CACHE, "pack.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_file = open(lock_path, "w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    return lock_file


def _release_pack_lock(lock_file) -> None:
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _write_pack_result(result_path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, "w") as f:
        json.dump(data, f)


def _worker_main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] != "--worker":
        return

    if len(argv) != 3:
        print("Usage: --worker <zip> <result_path>", file=sys.stderr)
        sys.exit(2)

    _, zip_path, result_path = argv

    try:
        service_id, metadata, service = zipfile_ok(zip=zip_path)

        if not service_id and not metadata and service:
            _write_pack_result(result_path, {"error": service})
        else:
            metadata_b64 = base64.b64encode(metadata.SerializeToString()).decode("ascii") if metadata else None
            if not isinstance(service, str):
                _write_pack_result(result_path, {"error": "Worker expected service directory, got protobuf."})
            else:
                _write_pack_result(result_path, {
                    "service_id": service_id,
                    "metadata_b64": metadata_b64,
                    "service_dir": service
                })
    except Exception as e:
        _write_pack_result(result_path, {"error": f"Worker exception: {str(e)}"})


if __name__ == "__main__":
    _worker_main()
