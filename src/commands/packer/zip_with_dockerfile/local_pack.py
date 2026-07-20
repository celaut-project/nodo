"""`nodo pack` — optional LOCAL (Docker) packer.

Enabled by ``packer.local: true`` in config.yaml (default False). This restores
nodo's original local build path — generate the service zip, hand it to the
gateway's ``Pack`` RPC, which builds it with nodo's isolated Docker toolchain and
streams back the service id, metadata and service directory — but wraps it in the
policy Josemi asked for:

  * Docker is provisioned on demand (``install_docker.sh``) the first time it's
    needed, isolated to the node (never the host Docker).
  * nodo's isolated Docker daemon is started right before the build and stopped
    right after it.
  * a command-level lock prevents two ``nodo pack`` runs at once.

When ``packer.local`` is False the node uses the packer-service client in
``pack.py`` instead; this module is never imported in that case.
"""
import fcntl
import os
import shutil
import sys
import threading
import time
from typing import Optional

from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.docker_dependency import (
    ensure_docker_installed,
    start_docker_daemon,
    stop_docker_daemon,
)

from src.commands.packer.zip_with_dockerfile.prepare_directory import prepare_directory
from src.commands.packer.zip_with_dockerfile.generate_service_zip import generate_service_zip

env_manager = ConfigManager()

GATEWAY_PORT = env_manager.get("GATEWAY_PORT")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")


# --------------------------------------------------------------------------- #
# Command-level concurrency lock: only one local `nodo pack` at a time.
# --------------------------------------------------------------------------- #
def _pack_lock_path() -> str:
    cache = env_manager.get("CACHE") or "/tmp"
    os.makedirs(cache, exist_ok=True)
    return os.path.join(cache, "nodo_pack_command.lock")


def _acquire_pack_lock():
    """Non-blocking exclusive lock. Returns the open file, or None if held."""
    lock_file = open(_pack_lock_path(), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        lock_file.close()
        return None
    return lock_file


def _release_pack_lock(lock_file) -> None:
    if not lock_file:
        return
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()


# --------------------------------------------------------------------------- #
# Restored gRPC client to the local gateway's Pack handler.
# --------------------------------------------------------------------------- #
def __spinner(event):
    """Spinner to show progress while the build runs."""
    spinner = ['|', '/', '-', '\\']
    messages = [
        "Processing... This might take a while.",
        "Processing... Please wait, the node is working.",
        "Processing... Almost there, please hold on.",
        "Processing... Still working, hang tight."
    ]
    idx = 0
    msg_idx = 0
    start_time = time.time()

    while not event.is_set():
        sys.stdout.write(f'\r{messages[msg_idx]} {spinner[idx]}')
        sys.stdout.flush()
        idx = (idx + 1) % len(spinner)
        if time.time() - start_time > 60:
            msg_idx = (msg_idx + 1) % len(messages)
            start_time = time.time()
        time.sleep(0.1)

    sys.stdout.flush()


def __pack(zip, node: str):
    import grpc
    from bee_rpc import client as grpcbb
    from protos import gateway_bee, celaut_pb2_grpc

    channel = grpc.insecure_channel(node)
    try:
        yield from grpcbb.client_grpc(
            method=celaut_pb2_grpc.GatewayStub(channel).Pack,
            input=grpcbb.Dir(dir=zip, _type=bytes),
            indices_serializer={0: bytes},
            indices_parser=gateway_bee.PackOutput_indices,
            partitions_message_mode_parser={1: True, 2: True, 3: False}
        )
    finally:
        channel.close()


def __on_peer(peer: str, service_zip_dir: str) -> Optional[str]:
    from bee_rpc import client as grpcbb
    from protos import celaut_pb2, pack_pb2
    from src.utils.hashing import get_configured_hash_spec, hash_stream

    _id: Optional[str] = None
    print(f'Starting packing your project on {peer}...')

    stop_event = threading.Event()
    spinner_thread = threading.Thread(target=__spinner, args=(stop_event,))
    spinner_thread.start()

    try:
        for b in __pack(zip=service_zip_dir, node=peer):
            if type(b) is pack_pb2.PackOutputServiceId:
                if not _id:
                    _id = b.id.hex()
            elif type(b) == celaut_pb2.Metadata and _id:
                # Metadata integrity validation.
                metadata_integrity_validation = [hash.type for hash in b.hashtag.hash]
                if len(metadata_integrity_validation) != len(set(metadata_integrity_validation)):
                    _msg = "Metadata integrity validation exception on pack command.\n"
                    for hash in list(b.hashtag.hash):
                        _msg += f"-  {hash.type.hex()}: {hash.value.hex()}\n"
                    print(_msg)
                    raise Exception(_msg)

                with open(f"{METADATA_REGISTRY}{_id}", "wb") as f:
                    f.write(b.SerializeToString())
            elif type(b) == grpcbb.Dir and b.type == pack_pb2.Service and _id:
                # b is the ServiceWithMeta grpc-bb cache directory.
                os.system(f"mv {b.dir} {REGISTRY}{_id}")
            elif type(b) == pack_pb2.PackOutputError:
                print(f"\nError in the compilation process: \n{b.message}")
                return None
            else:
                raise Exception('\nError with the packer output:' + str(b))
    finally:
        stop_event.set()
        spinner_thread.join()

    print('Compilation complete.')
    print('Service ID -> ', _id)
    print('\nValidating the content...')
    hash_spec = get_configured_hash_spec(env_manager)

    try:
        validated_hash_hex = hash_stream(
            grpcbb.read_multiblock_directory(f"{REGISTRY}{_id}/"),
            hash_spec
        ).hex()

        if validated_hash_hex == _id:
            print("Service id validated correctly.")
        else:
            print(
                "Service id mismatch after validation. "
                f"(validated result: {validated_hash_hex})"
            )

        min_block_size = env_manager.get("MIN_BUFFER_BLOCK_SIZE")
        if min_block_size < 10 ** 6:
            print(
                f"\n\n ALERT!! A buffer size that is too small (actual is {min_block_size}) may "
                "cause errors when generating the compressed version of the service, even without "
                "affecting its identifier and with correct validation of it. \n "
                "https://github.com/bee-rpc-protocol/bee-rpc/issues/7#issuecomment-2814172903"
            )
    except Exception as e:
        print(
            "Maybe it doesn't have blocks? validation will occur into an error due to "
            f"https://github.com/celaut-project/nodo/issues/38 \n Actually throws an exception: {str(e)}."
        )

    return _id


def __remove_path(path):
    if os.path.exists(path):
        (os.remove if os.path.isfile(path) else shutil.rmtree)(path)
        print(f"Removed: '{path}'")


def pack_local(directory: str) -> Optional[str]:
    """Build a project locally with nodo's isolated Docker toolchain."""
    # Guard against concurrent packs before doing any work.
    lock_file = _acquire_pack_lock()
    if lock_file is None:
        print(
            "\nAnother `nodo pack` is already running. Only one local pack can run "
            "at a time (nodo's isolated Docker daemon is shared). Wait for it to "
            "finish and try again."
        )
        return None

    daemon_started = False
    _id: Optional[str] = None
    is_remote = False
    try:
        # Provision the isolated Docker toolchain on demand, then start its daemon.
        ensure_docker_installed()
        start_docker_daemon()
        daemon_started = True

        is_remote, directory = prepare_directory(directory)
        service_zip_dir: str = generate_service_zip(project_directory=directory)

        ip, port = 'localhost', GATEWAY_PORT
        _id = __on_peer(peer=f"{ip}:{port}", service_zip_dir=service_zip_dir)

        if not _id:
            print(f"Packing produced no service id for {directory}.")

    except Exception as e:
        print(f"Exception packing {directory}: {e}")
        log.LOGGER(f"Local pack exception for {directory}: {e}")
    finally:
        if daemon_started:
            # Stop nodo's isolated Docker daemon once the pack completes.
            stop_docker_daemon()
        if is_remote:
            __remove_path(directory)
        _release_pack_lock(lock_file)

    return _id
