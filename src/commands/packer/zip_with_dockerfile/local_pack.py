"""`nodo pack` — optional LOCAL (rootless BuildKit) packer.

Enabled by ``packer.local: true`` in config.yaml (default False). This restores
nodo's original local build path — generate the service zip and build it with
nodo's rootless BuildKit toolchain by calling ``pack_zip()`` directly, in process,
streaming back the service id, metadata and service directory — but wraps it in
the policy Josemi asked for:

  * the builder is provisioned on demand (``install_buildkit.sh``) the first time
    it's needed, under MAIN_DIR (never a system-wide daemon).
  * nodo's rootless builder is started right before the build and stopped right
    after it. It runs as the invoking user, so no part of a pack needs sudo.
  * a command-level lock prevents two ``nodo pack`` runs at once.

Packing is fully local now: ``nodo pack`` calls ``pack_zip()`` in process instead
of going through the gateway's (now removed) ``Pack`` RPC — there is no gRPC
round-trip to the local gateway anymore.

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
from src.utils.builder_dependency import (
    ensure_builder_installed,
    start_builder,
    stop_builder,
)

from src.commands.packer.zip_with_dockerfile.prepare_directory import prepare_directory
from src.commands.packer.zip_with_dockerfile.generate_service_zip import generate_service_zip

env_manager = ConfigManager()

METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")


# --------------------------------------------------------------------------- #
# Command-level concurrency lock: only one local `nodo pack` at a time.
#
# A service that declares dependencies packs those dependencies first
# (generate_service_zip -> __export_registry -> pack -> pack_local). Those nested
# packs are part of the SAME `nodo pack` operation, so they must reuse the
# top-level pack's lock and its already-running rootless builder rather than
# trying (and failing) to re-acquire the single-holder command lock. `_pack_depth`
# tracks this reentrancy: only the outermost call owns the lock and the daemon.
# --------------------------------------------------------------------------- #
_pack_depth = 0


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
# In-process local packer.
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


def _pack_and_register(service_zip_dir: str) -> Optional[str]:
    """Build the prepared service zip in process and register the result.

    Calls ``pack_zip()`` directly and parses its PackOutput buffer stream locally
    (the same messages the gateway ``Pack`` RPC used to stream back), then writes
    the metadata and service directory into the node's registries. Returns the
    validated service id, or None on a packer error.
    """
    from bee_rpc import client as grpcbb
    from protos import celaut_pb2, pack_pb2, gateway_bee
    from src.packers.zip_with_dockerfile import pack_zip
    from src.utils.hashing import get_configured_hash_spec, hash_stream

    _id: Optional[str] = None
    print('Starting packing your project...')

    stop_event = threading.Event()
    spinner_thread = threading.Thread(target=__spinner, args=(stop_event,))
    spinner_thread.start()

    try:
        for b in grpcbb.parse_from_buffer(
            request_iterator=pack_zip(zip=service_zip_dir),
            indices=gateway_bee.PackOutput_indices,
            partitions_message_mode={1: True, 2: True, 3: False}
        ):
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

        min_block_size = env_manager.get("packer.MIN_BUFFER_BLOCK_SIZE")
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
    """Build a project locally with nodo's rootless BuildKit toolchain.

    Nested dependency packs (triggered from generate_service_zip while packing a
    service that declares yet-unpacked dependencies) are part of the same pack
    operation: they reuse the top-level pack's command lock and running builder
    instead of re-acquiring the single-holder lock (which would fail and cancel
    the dependency).
    """
    global _pack_depth
    nested = _pack_depth > 0

    # Only the top-level pack owns the command lock; nested dependency packs run
    # inside that same guarded context.
    lock_file = None
    if not nested:
        lock_file = _acquire_pack_lock()
        if lock_file is None:
            print(
                "\nAnother `nodo pack` is already running. Only one local pack can run "
                "at a time (nodo's rootless builder is shared). Wait for it to "
                "finish and try again."
            )
            return None

    _pack_depth += 1
    daemon_started = False
    _id: Optional[str] = None
    is_remote = False
    try:
        if not nested:
            # Provision the rootless BuildKit toolchain on demand, then start the
            # builder. It is started before generate_service_zip so nested
            # dependency packs build against the already-running builder.
            ensure_builder_installed()
            start_builder()
            daemon_started = True

        is_remote, directory = prepare_directory(directory)
        service_zip_dir: str = generate_service_zip(project_directory=directory)

        _id = _pack_and_register(service_zip_dir)

        if not _id:
            print(f"Packing produced no service id for {directory}.")

    except Exception as e:
        print(f"Exception packing {directory}: {e}")
        log.LOGGER(f"Local pack exception for {directory}: {e}")
    finally:
        _pack_depth -= 1
        if daemon_started:
            # Stop nodo's rootless builder once the whole pack completes.
            stop_builder()
        if is_remote:
            __remove_path(directory)
        if lock_file is not None:
            _release_pack_lock(lock_file)

    return _id
