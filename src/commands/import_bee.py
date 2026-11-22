import os
import shutil
from typing import Optional, Set
from protos import celaut_pb2
from bee_rpc.client import parse_from_buffer, read_from_file
from bee_rpc.reader import read_bee_file

from src.utils import partial_service, service_canonical
from src.utils.block_tree import block_tree
from src.utils.config import ConfigManager
from src.utils.hashing import get_configured_hash_spec, hash_stream
from src.utils.omit_filesystem import OmitSession, omit_session
from src.utils.service_content import read_service_content

env_manager = ConfigManager()

REGISTRY = env_manager.get("REGISTRY")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
VALIDATE_ON_IMPORT = env_manager.get("VALIDATE_ON_IMPORT")
BLOCKDIR = env_manager.get("BLOCKDIR")


# It's on utils.service_content too, but here we have other params...
def _compute_service_hash(service_path: str, hash_spec) -> str:
    return hash_stream(read_service_content(service_path=service_path), hash_spec).hex()


def _upsert_metadata_hash(metadata: celaut_pb2.Metadata, hash_id: bytes, hash_hex: str):
    hash_bytes = bytes.fromhex(hash_hex)
    for existing in metadata.hashtag.hash:
        if existing.type == hash_id:
            existing.value = hash_bytes
            return

    metadata.hashtag.hash.append(
        celaut_pb2.Metadata.HashTag.Hash(
            type=hash_id,
            value=hash_bytes,
        )
    )


def _remove_path(path: str):
    if not os.path.exists(path):
        return
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def _block_names() -> Set[str]:
    try:
        return set(os.listdir(BLOCKDIR))
    except OSError:
        return set()


def _discard(metadata_dir: Optional[str], service_dir: Optional[str], blocks_before: Set[str]):
    """Remove what an import that is refused wrote (audit F3).

    bee-rpc writes the blocks of the file to the block directory while it reads it, so before
    the id is checked. The blocks that the refused service needs, that were not in the block
    directory before the import, and that no service of the registry needs, are removed. If
    the blocks of a service of the registry cannot be read, no block is removed. The
    directories that the file was read into are removed too.
    """
    try:
        if service_dir and os.path.isdir(service_dir):
            added = [b for b in block_tree(service_dir, BLOCKDIR) if b not in blocks_before]
        else:
            added = []
        if added:
            used: Set[str] = set()
            for service in os.listdir(REGISTRY):
                service_path = os.path.join(REGISTRY, service)
                if os.path.isdir(service_path):
                    used.update(block_tree(service_path, BLOCKDIR))
            for block in added:
                if block not in used:
                    _remove_path(os.path.join(BLOCKDIR, block))
    except Exception as e:
        print(f"The blocks of the refused service are kept: {e}")
    for path in (metadata_dir, service_dir):
        if path:
            _remove_path(path)


#: The index of each message in a ``.celaut.bee`` file.
_METADATA_INDEX, _SERVICE_INDEX = 1, 2


def import_bee(
    path: str,
    integrity_hash: Optional[str] = None,
    keep_metadata: bool = False,
    omit_fs: Optional[bool] = None,
) -> Optional[str]:
    """Import the ``.celaut.bee`` at ``path``; the id of the service, or None.

    ``integrity_hash``: the id that the service must have; another one is not imported.
    ``keep_metadata``: the metadata (the tags) of a service that the node has already are not
    replaced by the ones of the file. A source that anybody can register must not rename a
    service that the node holds (audit N3).
    ``omit_fs``: keep the service without its filesystem (``misc.OMIT_FS``, the default).
    With ``VALIDATE_ON_IMPORT`` the filesystem is read to check the id, and it is not
    stored. A service that the node holds in full stays in full. A service that the node
    holds without its filesystem is completed when ``omit_fs`` is off.
    """
    if omit_fs is None:
        omit_fs = partial_service.omit_fs_enabled()
    # Get the current directory (where the "nodo" command is executed from)
    current_directory = os.getcwd()
    
    # Resolve the path: if it's relative, combine it with the current directory
    if not os.path.isabs(path):
        path = os.path.join(current_directory, path)
    
    # Convert the path to absolute (just in case)
    path = os.path.abspath(path)
    
    # Validate if the path exists
    if not os.path.exists(path):
        print(f"Error: The path '{path}' does not exist.")
        return
    
    # Validate if the path is a file
    if not os.path.isfile(path):
        print(f"Error: The path '{path}' is not a file.")
        return
    
    blocks_before = _block_names()
    metadata_dir = service_dir = None
    try:
        hash_spec = get_configured_hash_spec(env_manager)

        indices = {
            _METADATA_INDEX: celaut_pb2.Metadata,
            _SERVICE_INDEX: celaut_pb2.Service,
        }
        session: Optional[OmitSession] = None
        if omit_fs:
            # The filesystem is read and not stored. With VALIDATE_ON_IMPORT the session
            # hashes it, so the id is checked as if it were stored.
            session = OmitSession(
                service_index=_SERVICE_INDEX,
                spec=hash_spec if VALIDATE_ON_IMPORT else None,
            )
            with omit_session(session):
                it = parse_from_buffer(
                    request_iterator=session.tap(read_bee_file(path)),
                    indices=indices,
                    partitions_message_mode=False,
                )
                metadata_dir = next(it).dir
                service_dir = next(it).dir
        else:
            # Read the file using bee_rpc.client
            it = read_from_file(path=path, indices=indices)

            # Extract the metadata directory and parse the metadata
            metadata_dir = next(it).dir
            service_dir = next(it).dir
        omitted = bool(session and session.omitted)
        if omitted and not VALIDATE_ON_IMPORT:
            print("The id of the service is not checked over its filesystem "
                  "(misc.VALIDATE_ON_IMPORT is off).")
        metadata = celaut_pb2.Metadata()
        metadata.ParseFromString(open(metadata_dir, "rb").read())
        
        # Find the configured service hash in metadata first.
        service_hash = None
        for _hash in metadata.hashtag.hash:
            if _hash.type == hash_spec.id_bytes:
                service_hash = _hash.value.hex()
                break

        if VALIDATE_ON_IMPORT:
            if session is not None:
                calculated_hash = session.digest().hex()
            else:
                calculated_hash = _compute_service_hash(service_path=service_dir, hash_spec=hash_spec)

            if service_hash != calculated_hash:
                _upsert_metadata_hash(
                    metadata=metadata,
                    hash_id=hash_spec.id_bytes,
                    hash_hex=calculated_hash,
                )
                service_hash = calculated_hash
        
        if integrity_hash:
            if integrity_hash != service_hash:
                print(
                    f"Integrity hash mismatch. "
                    f"Expected: {service_hash}, received: {integrity_hash}. "
                    f"Ensure that the integrity hash uses the {hash_spec.name} algorithm."
                )
                _discard(metadata_dir, service_dir, blocks_before)
                return
        
        # The id of a service is the hash of its bytes, so they must be the canonical encoding
        # (docs/CANONICAL_ENCODING.md). `misc.CANONICAL_SERVICE: warn` imports it anyway.
        try:
            service_canonical.enforce_spec_path(service_dir, "The service to import")
        except service_canonical.ServiceNotCanonical as e:
            print(str(e))
            _discard(metadata_dir, service_dir, blocks_before)
            return

        # Only now is the id known: the one that the metadata says may not be the one of the
        # content (VALIDATE_ON_IMPORT replaced it above).
        service_saved = os.path.exists(os.path.join(REGISTRY, service_hash))

        # Move metadata to the metadata registry
        metadata_destination = os.path.join(METADATA_REGISTRY, service_hash)
        if not (keep_metadata and os.path.exists(metadata_destination)):
            with open(metadata_destination, "wb") as metadata_file:
                metadata_file.write(metadata.SerializeToString())
        _remove_path(metadata_dir)
        
        # Move or remove the service file based on whether it's already saved
        if not service_saved:
            service_destination = os.path.join(REGISTRY, service_hash)
            shutil.move(service_dir, service_destination)
            if omitted:
                partial_service.mark_partial(service_hash, REGISTRY)
        else:
            if not omitted and partial_service.is_partial(service_hash, REGISTRY):
                # The node held the service without its filesystem and now has all the
                # blocks: the entry that is there is the same specification.
                partial_service.clear_partial(service_hash, REGISTRY)
            _remove_path(service_dir)
        
        print("\n\nService imported successfully.")
        return service_hash
    
    except Exception as e:
        print(f"Error importing service: {e}")
        return
