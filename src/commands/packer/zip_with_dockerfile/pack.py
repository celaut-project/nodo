import os, sys, time, threading, shutil
from typing import Optional
import grpc
from bee_rpc import client as grpcbb

from protos import celaut_pb2, pack_pb2, gateway_bee, celaut_pb2_grpc
from src.commands.packer.zip_with_dockerfile.prepare_directory import prepare_directory
from src.commands.packer.zip_with_dockerfile.generate_service_zip import generate_service_zip
from src.database.access_functions.peers import get_peer_ids, get_peer_directions
from src.utils.config import ConfigManager
from src.utils.hashing import get_configured_hash_spec, hash_stream

env_manager = ConfigManager()

GATEWAY_PORT = env_manager.get("GATEWAY_PORT")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")
# Optional: when set, `nodo pack` uploads registry-hash dependencies to this
# remote packer service before packing, so the packer can resolve them (the
# packer's own registry is otherwise empty). Empty -> local packing only.
PACKER_SERVICE_URL = env_manager.get("PACKER_SERVICE_URL") or ""

def __spinner(event):
    """Spinner function to show progress while the main task runs."""
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
        # Show spinner animation
        sys.stdout.write(f'\r{messages[msg_idx]} {spinner[idx]}')
        sys.stdout.flush()
        idx = (idx + 1) % len(spinner)

        # Update message every minute
        if time.time() - start_time > 60:
            msg_idx = (msg_idx + 1) % len(messages)
            start_time = time.time()

        time.sleep(0.1)  # Adjust speed of spinner

    sys.stdout.flush()



def __pack(zip, node: str):
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


def __on_peer(peer: str, service_zip_dir: str) -> str:
    _id: Optional[str] = None
    print(f'Starting packing your project on {peer}...')
    
    # Create an event to control the spinner thread
    stop_event = threading.Event()
    spinner_thread = threading.Thread(target=__spinner, args=(stop_event,))
    spinner_thread.start()
    
    try:
        for b in __pack(
                zip=service_zip_dir,
                node=peer
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
                # b is ServiceWithMeta grpc-bb cache directory.
                os.system(f"mv {b.dir} {REGISTRY}{_id}")
            elif type(b) == pack_pb2.PackOutputError:
                print(f"\nError in the compilation process: \n{b.message}")
                return
            else:
                raise Exception('\nError with the packer output:' + str(b))

    except Exception as e:
        raise e

    finally:
        # Stop the spinner when the process completes
        stop_event.set()
        spinner_thread.join()

    print('Compilation complete.')
    print('Service ID -> ', _id)
    print('\nValidating the content...')
    hash_spec = get_configured_hash_spec(env_manager)
    validated_hash_hex = ""

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
        if min_block_size < 10 **6:
            print(f"\n\n ALERT!! It has been detected that a buffer size that is too small (actual is {min_block_size}) may cause errors when generating the compressed version of the service, even without affecting its identifier and with correct validation of it. \n https://github.com/bee-rpc-protocol/bee-rpc/issues/7#issuecomment-2814172903")
            
    except Exception as e:
        print(f"Maybe it doesn't have blocks? validation will occurr into an error due to https://github.com/celaut-project/nodo/issues/38 \n Actually throws an exception: {str(e)}.")
    
    return _id


def __remove_path(path):
    if os.path.exists(path):
        (os.remove if os.path.isfile(path) else shutil.rmtree)(path)
        print(f"Removed: '{path}'")


def pack(directory: str) -> str:
    _id = None
    is_remote, directory = prepare_directory(directory)  # TODO Better approach, generator: return only path and finally remove if remote.

    # When packing against a remote packer service, its registry is empty, so
    # any registry-hash dependency this project declares must be pushed there
    # first. Resolve each dependency against THIS nodo's registry (raising a
    # clear error if one is missing) and upload the registry-hash ones.
    if PACKER_SERVICE_URL:
        from src.commands.packer.zip_with_dockerfile.packer_service_client import (
            resolve_and_upload_dependencies,
        )
        print(f"Resolving dependencies against packer service {PACKER_SERVICE_URL} ...")
        summary = resolve_and_upload_dependencies(
            project_directory=directory,
            packer_service_url=PACKER_SERVICE_URL,
        )
        if summary["uploaded"]:
            print(f"Uploaded dependencies: {', '.join(summary['uploaded'])}")
        if summary["already_present"]:
            print(f"Dependencies already on packer: {', '.join(summary['already_present'])}")

    service_zip_dir: str = generate_service_zip(
        project_directory=directory
    )

    try:
        ip, port = None, None
        if False:  # TODO; control exceptions and try others; and environment variable PACK_LOCAL_FIRST
            for peer_id in list(get_peer_ids()):
                for _ip, _port in get_peer_directions(peer_id=peer_id):
                    ip, port = _ip, _port
        if not ip or not port:
            ip, port = 'localhost', GATEWAY_PORT
        _id = __on_peer(peer=f"{ip}:{port}", service_zip_dir=service_zip_dir)
        
        if not _id: 
            _msg = f"Any id for {directory}"
            print(_msg) 
            raise Exception(_msg)
    
    except Exception as e:
        print(f"Excepton packing {directory}: {e}")
        
    finally:
        # __remove_path(service_zip_dir)
        
        if is_remote: 
            __remove_path(directory)

    return _id
