import base64
import fcntl
import posixpath
import stat
from typing import Generator, List, Tuple

from src.utils import logger as log
import json
import os, shutil, subprocess, platform, sys, uuid
import src.manager.resources as resources
from bee_rpc import client as grpcbb
from bee_rpc.utils import Enviroment, modify_env, block_pointer, hash_types_for_packing
from bee_rpc import buffer_pb2, block_builder
from protos import celaut_pb2 as celaut, pack_pb2, gateway_bee
from src.utils.config import ConfigManager
from src.packers.service_json import populate_possible_environment_workloads
from src.utils.hashing import (
    BLAKE2B_ID, HASH_SPECS, SHA3_256_ID, get_configured_hash_spec, hash_stream_many,
)
from src.utils.arch_guard import ensure_native_arch
# PACKER_SUPPORTED_ARCHITECTURES lives in the container-free architectures module.
# BUILDCTL_COMMAND/BUILDKIT_ENV point this worker at nodo's own rootless BuildKit
# builder and come from the buildkit_env helper (no container library import), so
# importing this worker never drags a builder into the CH-only runtime.
from src.utils.architectures import PACKER_SUPPORTED_ARCHITECTURES
from src.utils.buildkit_env import BUILDCTL_COMMAND, BUILDKIT_ENV
from src.utils.filesystem_xattrs import (
    describe_mode_type,
    encode_filesystem_metadata_xattrs,
    implicit_directory_metadata,
    is_supported_filesystem_entry_mode,
    metadata_from_lstat,
    metadata_from_tarinfo,
)
from src.utils.verify import calculate_hashes_by_stream
from src.utils.config import ConfigManager
from src.manager.resources import IOBigData

env_manager = ConfigManager()

CACHE = env_manager.get("CACHE")
BLOCKDIR = env_manager.get("BLOCKDIR")
# Defaults keep the local packer working on configs that predate these keys
# (e.g. nodes upgraded from a Docker-free build) — packer.local is opt-in and its
# config section may not exist yet.
# Measured, not guessed. Peak memory has three parts, and the reservation needs
# all three because the block threshold decides which one dominates:
#   * the inlined bytes, at ~5.9x (a real 1.2 GB image: 886 MB inlined, 4950 MB
#     peak). Against the *total* exported size the same measurements ranged from
#     0.6x to 6.3x, which is why nothing here is proportional to that;
#   * ~7 kB per block, which is what a low threshold turns almost every file
#     into (100/400/1600 blocks with nothing inlined: 4.1/6.7/14.4 MB);
#   * a fixed ~30 MB, the worker interpreter itself.
PACKER_MEMORY_SIZE_FACTOR = env_manager.get("PACKER_MEMORY_SIZE_FACTOR", 6.0) or 6.0
PACKER_MEMORY_PER_BLOCK = env_manager.get("PACKER_MEMORY_PER_BLOCK", 10_000) or 10_000
PACKER_MEMORY_OVERHEAD = env_manager.get("PACKER_MEMORY_OVERHEAD", 40_000_000) or 40_000_000
# How long a pack waits for that memory before giving up.
WAIT_FOR_UNLOCK_MEMORY = env_manager.get("packer.WAIT_FOR_UNLOCK_MEMORY", 300) or 300
SAVE_ALL = env_manager.get("SAVE_ALL", False)
MIN_BUFFER_BLOCK_SIZE = env_manager.get("packer.MIN_BUFFER_BLOCK_SIZE")
# Name of the Dockerfile inside the project directory. BuildKit's dockerfile
# frontend defaults to "Dockerfile" too; this only exists to make it overridable.
DOCKERFILE_NAME = env_manager.get("packer.buildkit.DOCKERFILE_NAME", "Dockerfile") or "Dockerfile"

# Ensure bee_rpc uses the configured cache and block directories.
if CACHE:
    os.makedirs(CACHE, exist_ok=True)
if BLOCKDIR:
    os.makedirs(BLOCKDIR, exist_ok=True)
    modify_env(cache_dir=CACHE, block_dir=BLOCKDIR)


def _normalize_tar_member_path(name: str) -> str:
    # Tar member names are posix paths, sometimes "./bin/bash", sometimes
    # "bin/bash", sometimes "bin/" for a directory. Normalize to the same
    # "bin/bash" shape recursive_parsing's own (directory + b_name) builds, so
    # a lookup by path always hits. The root entry ("." or "./") normalizes to
    # "" and is filtered out by the caller — it has no corresponding branch.
    normalized = posixpath.normpath(name).lstrip("/")
    return "" if normalized == "." else normalized


# Every service carries these digests regardless of `hashing.HASH`, so any
# node -- whatever algorithm it is configured with -- can resolve or verify a
# service it did not pack itself (get_service_hex_main_hash falls back to
# SHA3_256; a peer whose hashing.HASH is BLAKE2B needs that entry the same
# way). SHA3_256 is bee-rpc's own block-addressing hash (Enviroment.hash_type)
# and hashing.py's DEFAULT_HASH_NAME; BLAKE2B joins it for the same reason
# verify.py's calculate_hashes does -- so a service stays resolvable if some
# node's hashing.HASH is ever set to it, without a repack.
COMPANION_HASH_IDS = (SHA3_256_ID, BLAKE2B_ID)


def packing_memory_estimate(inline_len: int, block_count: int = 0) -> int:
    """RAM to reserve for a pack, from what it will actually hold.

    An inlined byte is expensive: the filesystem message, its serializations and
    the buffer built from them all coexist at the peak, so each one costs
    several times over. A file stored as a block is streamed to disk and costs
    only its bookkeeping. So an image of mostly large files packs in a fraction
    of what its size suggests, and one of mostly small files in several times
    it -- which is why neither the exported size nor a single factor over it
    predicts anything.

    Both terms are needed because packer.MIN_BUFFER_BLOCK_SIZE decides which one
    dominates: raise it and almost everything is inlined, lower it and almost
    everything is a block.
    """
    return (
        int(float(PACKER_MEMORY_SIZE_FACTOR) * inline_len)
        + int(PACKER_MEMORY_PER_BLOCK) * block_count
        + int(PACKER_MEMORY_OVERHEAD)
    )


def _install_as_block(block_id: bytes, directory: str) -> bytes:
    """Move a freshly built multiblock directory into the block registry.

    Blocks are content-addressed, so an identical filesystem packed twice lands
    on a name that is already there and the second copy is simply dropped. The
    move goes through a temporary name inside the registry so the block appears
    under its own id only once every part of it is in place: a reader that found
    a half-moved directory would expand it into short content with no error.
    """
    destination: str = os.path.join(BLOCKDIR, block_id.hex())
    source: str = directory.rstrip(os.sep)

    if os.path.exists(destination):
        shutil.rmtree(source, ignore_errors=True)
        return block_id

    staging: str = destination + '.tmp-' + uuid.uuid4().hex
    shutil.move(source, staging)
    try:
        os.rename(staging, destination)
    except OSError:
        # Lost the race against another packer storing the same filesystem.
        shutil.rmtree(staging, ignore_errors=True)
        if not os.path.exists(destination):
            raise
    return block_id


class ZipContainerPacker:
    def __init__(self, path, aux_id):
        self.blocks: List[bytes] = []
        # The block the container filesystem is stored as; see parseFilesys.
        self.filesystem_block: bytes = b''
        self.buffer_len: int = 0
        self.inline_len: int = 0
        self.block_count: int = 0
        self.service = pack_pb2.Service()
        self.metadata = celaut.Metadata()
        self.path = path
        self.json = json.load(open(self.path + "service.json", "r"))
        self.aux_id = aux_id
        self.error_msg = None
        self._tar_metadata_by_path = {}
        self._validate_service_json_shape()

        arch = None
        for a in PACKER_SUPPORTED_ARCHITECTURES:
            if self.json.get('architecture') in a: arch = a[0]

        if not arch: raise Exception("Can't pack this service, not supported architecture.")

        # 1. Architecture detection
        host_arch = platform.machine().lower()
        target_arch = arch
        ensure_native_arch(target_arch, context="packer build")

        # 2. Prepare output path
        dest_path = os.path.join(CACHE, self.aux_id, "filesystem")
        os.makedirs(dest_path, exist_ok=True)
        tar_path = os.path.join(CACHE, self.aux_id, "filesystem.tar")

        # 3. Construct secure command
        # BuildKit is driven directly instead of through `docker buildx`: buildx is
        # only a front end for it, and the standalone daemon runs rootless as our
        # own user, so no step of a pack needs sudo. There is no builder to create
        # or bootstrap either — nodo starts buildkitd around the pack
        # (bash/start_buildkit_daemon.sh) with the host network, which is what the
        # old `--network host` buildx builder existed to provide.
        build_cmd = BUILDCTL_COMMAND + [
            "build",
            "--frontend", "dockerfile.v0",
            "--local", f"context={self.path}",
            "--local", f"dockerfile={self.path}",
            "--opt", f"filename={DOCKERFILE_NAME}",
            "--opt", f"platform={target_arch}",
            "--progress", "plain",
            "--no-cache",
            "--output", f"type=tar,dest={tar_path}",
        ]

        # 4. Secure execution
        try:
            log.LOGGER(f"Starting build {target_arch} on host {host_arch}...")
            
            process = subprocess.Popen(
                build_cmd, 
                cwd=self.path, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                text=True,
                env=BUILDKIT_ENV,
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
                members = tar.getmembers()
                tar.extractall(path=dest_path, members=members)
                # `tarfile.extractall` only chowns to the tar's uid/gid when run as
                # root; unprivileged (our case, always, now that the builder is
                # rootless) every entry lands owned by us regardless of what the
                # tar says. Those uid/gid values feed the content-addressed service
                # hash, so hashing what's on disk after extraction would make the
                # id depend on who ran the pack. Keep the tar's own metadata
                # instead, keyed by the path as parseContainer will look it up.
                self._tar_metadata_by_path = {
                    _normalize_tar_member_path(member.name): metadata_from_tarinfo(member)
                    for member in members
                    if _normalize_tar_member_path(member.name)
                }
            os.remove(tar_path)

            log.LOGGER("Filesystem export completed successfully.")
            
            # Two sizes, because they answer different questions. `buffer_len`
            # is everything the image holds; `inline_len` is only the part that
            # ends up inside the filesystem message, which is what the pack
            # costs in memory. A file at or over MIN_BUFFER_BLOCK_SIZE is
            # streamed into a block on disk by parseFilesys and never held.
            total_size = 0
            inline_size = 0
            block_count = 0
            for dirpath, _, filenames in os.walk(dest_path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        size = os.path.getsize(fp)
                        total_size += size
                        if size < MIN_BUFFER_BLOCK_SIZE:
                            inline_size += size
                        else:
                            block_count += 1
            self.buffer_len = total_size
            self.inline_len = inline_size
            self.block_count = block_count

        except Exception as e:
            self.error_msg = f"Unexpected error during build: {str(e)}"
            log.LOGGER(self.error_msg)
            return

        # Check first tag for use as name
        self.tag = self.json.get("tag")

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

        def parseFilesys() -> celaut.Metadata.HashTag:
            # File system is already exported to filesystem/ by BuildKit
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
                    branch_host_path = host_dir + directory + b_name
                    try:
                        branch_stat = os.lstat(branch_host_path)
                    except OSError as e:
                        raise RuntimeError(
                            f"Unable to read filesystem metadata for '{directory + b_name}': {e}"
                        ) from e

                    if not is_supported_filesystem_entry_mode(branch_stat.st_mode):
                        raise RuntimeError(
                            "Unsupported filesystem entry type for "
                            f"'{directory + b_name}': "
                            f"{describe_mode_type(branch_stat.st_mode)} "
                            f"(mode={oct(branch_stat.st_mode)})"
                        )
                    # Prefer the tar's own record of this entry over the extracted
                    # copy on disk: extractall only restores uid/gid from the tar
                    # when run as root, so an unprivileged extraction (always, now
                    # that the builder is rootless) would otherwise stamp the
                    # content-addressed hash with the packer's own uid/gid instead
                    # of the image's. A directory tarfile only created implicitly,
                    # as a deeper entry's parent, has no member of its own; every
                    # packer fabricates the same synthetic metadata for it. Anything
                    # else missing from the tar (there should be nothing) falls back
                    # to the previous, best-effort behavior.
                    tar_metadata = self._tar_metadata_by_path.get((directory + b_name).lstrip("/"))
                    if tar_metadata is not None:
                        branch_metadata = tar_metadata
                    elif stat.S_ISDIR(branch_stat.st_mode):
                        branch_metadata = implicit_directory_metadata()
                    else:
                        branch_metadata = metadata_from_lstat(branch_stat)
                    encode_filesystem_metadata_xattrs(branch.xattrs, branch_metadata)

                    # It's a link.
                    if os.path.islink(branch_host_path):
                        branch.link.dst = directory + b_name
                        branch.link.src = os.path.realpath(branch_host_path)[
                                          len(host_dir):] if host_dir in os.path.realpath(
                            branch_host_path) else os.path.realpath(branch_host_path)
                    # Device node (block/char): represent as file placeholder and recover via xattrs in CH build.
                    elif branch_metadata.is_device:
                        branch.file = b""
                    # It's a file.
                    elif os.path.isfile(branch_host_path):
                        if os.path.getsize(branch_host_path) < MIN_BUFFER_BLOCK_SIZE:
                            with open(branch_host_path, 'rb') as file:
                                branch.file = file.read()
                        else:
                            block_hash, _ = block_builder.create_block(
                                file_path=branch_host_path,
                                copy=True
                            )
                            # No hash type in the pointer: the filesystem is stored as
                            # a block of its own, so these sit one level down and take
                            # their types from the pointer that names it. An image of
                            # a few thousand large files would otherwise repeat the
                            # same 32 bytes a few thousand times for no information.
                            branch.file = block_pointer(
                                block_id=block_hash, omit_types=True
                            ).SerializeToString()
                            if block_hash not in self.blocks:
                                self.blocks.append(block_hash)
                    # It's a folder.
                    elif os.path.isdir(branch_host_path):
                        branch.filesystem.CopyFrom(
                            recursive_parsing(directory=directory + b_name + '/')
                        )
                    else:
                        raise RuntimeError(
                            "Unsupported filesystem entry kind for "
                            f"'{directory + b_name}' after metadata capture."
                        )
                    filesystem.branch.append(branch)
                return filesystem
            # The filesystem is stored as one block of its own rather than
            # inlined into the spec, so that reading the spec -- to answer what
            # ports it exposes, what it costs, whether it needs a parent-exported
            # directory -- does not mean reading the whole rootfs. Only the build
            # expands it. Both shapes expand to the same bytes, so this does not
            # change the service id (see src/utils/container_filesystem.py).
            #
            # The multiblock directory built here is the one that used to be
            # built purely to hash the filesystem and then thrown away
            # (delete_directory=True): its id already *is* that hash, so keeping
            # it costs nothing and the metadata hash below is unchanged. A
            # filesystem with no file over the block threshold takes this path
            # too -- with no blocks to substitute, the object's id is the plain
            # sha3_256 of its serialization, exactly what the old
            # `calculate_hashes` branch produced.
            # `inherited` tells the builder what the pointers above leave unsaid, so
            # it can read them at all. It does not change what this block expands to
            # -- a pointer is replaced by its block's content either way -- so the
            # filesystem block's id, and the service id above it, are the same as
            # they would be with every type spelled out.
            self.filesystem_block = _install_as_block(
                *block_builder.build_multiblock(
                    pf_object_with_block_pointers=recursive_parsing(directory="/"),
                    blocks=self.blocks,
                    inherited=hash_types_for_packing()
                )
            )

            return celaut.Metadata.HashTag(
                hash=calculate_hashes_by_stream(
                    value=grpcbb.read_block(
                        block_id=self.filesystem_block.hex(),
                        ignore_blocks=True
                    )
                )
            )
    
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

        # Possible descendant workloads. Each scenario is one independent
        # worst-case concurrent execution the service may trigger through its
        # descendants (not cumulative, no ordering). Spec-only: nodo does not
        # interpret/validate these here — that is future scheduler work (#163).
        populate_possible_environment_workloads(
            self.service,
            self.json.get("possible_environment_workload", []),
        )


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

        # Envs: `service.json` may declare environment-variable NAMES via `envs`.
        # Per-variable `<ENV>.field` descriptors are NOT embedded: there is no
        # target field for them in the packed schema. `Service.Api` has no
        # `environment_variables` field (it lives on `celaut.Container`), and
        # `pack.proto`'s Container omits it too, so the previous write crashed
        # with AttributeError whenever a `.field` descriptor was actually
        # present. Embedding descriptors would require adding the field to
        # pack.proto first (deferred design decision — see issue #192).

        if not self.json.get('api'): return
        
        for item in self.json.get('api'):  # iterate slots.
            slot = celaut.Service.Api.Slot()
            slot.port = item.get('port')
            transport_tags = item.get("transport")
            if transport_tags is None:
                transport_tags = ["tcp"]  # Default to TCP if not specified, as it's the most common protocol for API slots.
                # raise ValueError(
                #     f"service.json api slot port={slot.port}: missing required 'transport' field."
                # )
            if isinstance(transport_tags, str):
                transport_tags = [transport_tags]
            if not isinstance(transport_tags, list) or not transport_tags:
                raise ValueError(
                    f"service.json api slot port={slot.port}: 'transport' must be a non-empty string or list of strings."
                )
            slot.transport.tags.extend([str(tag) for tag in transport_tags if str(tag).strip()])
            if not slot.transport.tags:
                raise ValueError(
                    f"service.json api slot port={slot.port}: 'transport' contains no valid tags."
                )
            slot.protocol_stack.append(
                celaut.Service.Api.Protocol(
                    tags=item.get('protocol')
                )
            )
            # `gas_amount_per_call` was renamed to `mu_per_call` with the pricing
            # rework. Rejecting the old key rather than ignoring it is deliberate: a
            # service.json that still uses it would otherwise pack with no per-call
            # price at all, i.e. silently free.
            if "gas_amount_per_call" in item:
                raise ValueError(
                    f"service.json api slot port={slot.port}: 'gas_amount_per_call' was "
                    "renamed to 'mu_per_call' (amounts in MU, the node's unit of account). "
                    "See docs/PRICING.md."
                )
            for method, amount_mu in item.get("mu_per_call", {}).items():
                slot.mu_per_call[method].n = str(amount_mu)
            self.service.api.slot.append(slot)
            
    def parseNetwork(self):
        if self.json.get('network'):
            for json_network in self.json.get("network", []):
                network = celaut.Service.Network()
                network.tags.extend(json_network['tags'])
                network.prose = json_network['prose']
                self.service.network.append(network)

    def save(self) -> Tuple[str, celaut.Metadata, str]:
        # What gets stored is a `celaut.Service`, the schema every reader of a
        # packed service already uses. `pack.Service` exists so that bee-rpc can
        # see *into* the filesystem while it is being built -- a message field is
        # walked for the block pointers of individual large files, an opaque
        # bytes field is not. That visibility is what the filesystem block needed
        # one level down, in parseFilesys; here the opposite is required, since
        # the spec must carry the filesystem as a pointer and bee-rpc only treats
        # a whole `bytes` field as one. The two schemas are wire-compatible, so
        # this re-reads the same bytes under the schema that says `bytes`.
        spec = celaut.Service()
        spec.ParseFromString(self.service.SerializeToString())
        # The top of what gets written to disk, so it states the hash type outright:
        # there is nothing above it to inherit from, and this is the pointer another
        # node has to be able to read without sharing this one's configuration.
        # Everything below it -- the per-file pointers in parseFilesys -- inherits
        # from here and leaves the type out.
        spec.container.filesystem = block_pointer(
            block_id=self.filesystem_block
        ).SerializeToString()

        # Always build a multiblock directory so the service is returned as a path.
        _, service_directory = block_builder.build_multiblock(
            pf_object_with_block_pointers=spec,
            blocks=[self.filesystem_block]
        )

        # Every digest this node has to record, hashed here from the service's
        # expanded content rather than taken from the id build_multiblock
        # returns. That id is always sha3_256 -- bee-rpc has no notion of the
        # `hashing.HASH` this node is configured with -- so borrowing it as a
        # companion entry below only happened to be right, and would have gone
        # on looking right while labelling whatever the library hashed with.
        # The content is the whole service, so it is read once and fed to
        # every hasher this needs -- the configured algorithm plus whichever
        # companions it is not already one of.
        hash_spec = get_configured_hash_spec(env_manager)
        required_specs = {hash_spec.id_bytes: hash_spec}
        for companion_id in COMPANION_HASH_IDS:
            required_specs.setdefault(companion_id, HASH_SPECS[companion_id])
        digests = hash_stream_many(
            grpcbb.read_multiblock_directory(directory=service_directory),
            list(required_specs.values())
        )
        configured_digest = digests[hash_spec.id_bytes]
        service_id: str = configured_digest.hex()

        updated = False
        for item in self.metadata.hashtag.hash:
            if item.type == hash_spec.id_bytes:
                item.value = configured_digest
                updated = True
                break
        if not updated:
            self.metadata.hashtag.hash.extend(
                [celaut.Metadata.HashTag.Hash(
                    type=hash_spec.id_bytes,
                    value=configured_digest
                )]
            )

        for companion_id in COMPANION_HASH_IDS:
            if (
                companion_id != hash_spec.id_bytes
                and not any(item.type == companion_id for item in self.metadata.hashtag.hash)
            ):
                self.metadata.hashtag.hash.extend(
                    [celaut.Metadata.HashTag.Hash(
                        type=companion_id,
                        value=digests[companion_id]
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

def ok(path, aux_id) -> Tuple[str, celaut.Metadata, str]:
    spec_file = ZipContainerPacker(path=path, aux_id=aux_id)
    
    # Check if there was an error during initialization
    if spec_file.error_msg:
        return "", None, spec_file.error_msg

    iobd = IOBigData()
    iobd.log_snapshot(context=f"pack-worker:start aux_id={aux_id}")
    _memory = packing_memory_estimate(
        inline_len=spec_file.inline_len, block_count=spec_file.block_count)
    log.LOGGER(
        f"Try to lock {_memory / (1024**2):.2f} MB of RAM for packing process "
        f"(inlined: {spec_file.inline_len / (1024**2):.2f} MB of "
        f"{spec_file.buffer_len / (1024**2):.2f} MB exported, in "
        f"{spec_file.block_count} blocks). "
        f"RAM avaliable before locking: {iobd.get_ram_avaliable() / (1024**2):.2f} MB"
    )
    try:
        with resources.mem_manager(len=_memory, timeout=WAIT_FOR_UNLOCK_MEMORY):
            iobd.log_snapshot(context=f"pack-worker:after-lock aux_id={aux_id} requested={_memory}")
            log.LOGGER(f"RAM locked successfully for packing process. RAM avaliable after locking: {iobd.get_ram_avaliable() / (1024**2):.2f} MB")
            spec_file.parseContainer()
            spec_file.parseApi()
            spec_file.parseNetwork()

            identifier, metadata, service = spec_file.save()
            iobd.log_snapshot(context=f"pack-worker:before-unlock aux_id={aux_id} service_id={identifier}")
    except TimeoutError:
        # Without a deadline this waited forever, and a pack that never returns
        # is indistinguishable from one that is still working: the parent's
        # subprocess.run() simply never comes back. Say so instead.
        message = (
            f"Timed out after {WAIT_FOR_UNLOCK_MEMORY}s waiting for "
            f"{_memory / (1024**2):.2f} MB of RAM to pack this service."
        )
        log.LOGGER(message)
        iobd.log_snapshot(context=f"pack-worker:timeout aux_id={aux_id} requested={_memory}")
        os.system('rm -rf ' + CACHE + aux_id + '/')
        return "", None, message

    iobd.log_snapshot(context=f"pack-worker:after-unlock aux_id={aux_id} service_id={identifier}")
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
    IOBigData().log_snapshot(context=f"pack-daemon:before-worker zip={zip}")
    lock_file = _acquire_pack_lock()
    try:
        result_path = os.path.join(CACHE, f"pack_result_{uuid.uuid4().hex}.json")
        main_dir = env_manager.get("MAIN_DIR") or os.getcwd()
        cmd = [
            sys.executable, "-m", "src.packers.zip_with_dockerfile",
            "--worker", zip, result_path
        ]
        proc = subprocess.run(cmd, cwd=main_dir)
        IOBigData().log_snapshot(
            context=f"pack-daemon:after-worker zip={zip} returncode={proc.returncode}"
        )

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
