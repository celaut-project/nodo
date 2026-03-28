import hashlib
from typing import Generator, List

from src.utils.hashing import SHA3_256_ID, SHA3_256, SHAKE_256_ID, SHAKE_256, get_configured_hash_id
from protos.celaut_pb2 import Metadata


def calculate_hashes_by_stream(value: Generator[bytes, None, None]) -> List[Metadata.HashTag.Hash]:
    sha3_256 = hashlib.sha3_256()
    shake_256 = hashlib.shake_256()
    for chunk in value:
        sha3_256.update(chunk)
        shake_256.update(chunk)

    return [
        Metadata.HashTag.Hash(
            type=SHA3_256_ID,
            value=sha3_256.digest()
        ),
        Metadata.HashTag.Hash(
            type=SHAKE_256_ID,
            value=shake_256.digest(32)
        )
    ]


def calculate_hashes(value: bytes) -> List[Metadata.HashTag.Hash]:
    return [
        Metadata.HashTag.Hash(
            type=SHA3_256_ID,
            value=SHA3_256(value)
        ),
        Metadata.HashTag.Hash(
            type=SHAKE_256_ID,
            value=SHAKE_256(value)
        )
    ]


# Return the configured main service hash on hexadecimal format.
def get_service_hex_main_hash(
        metadata: Metadata = None,
        other_hashes: list = None
) -> str:
    configured_hash_id = get_configured_hash_id()

    # Find if it has the hash.
    if other_hashes is None:
        other_hashes = []
    if metadata is None:
        metadata = Metadata()

    all_hashes = list(metadata.hashtag.hash) + other_hashes
    for hash in all_hashes:
        if hash.type == configured_hash_id:
            return hash.value.hex()

    # Compatibility fallback for old metadata that has not been migrated yet.
    for hash in all_hashes:
        if hash.type == SHA3_256_ID:
            return hash.value.hex()

    if all_hashes:
        return all_hashes[0].value.hex()


def get_service_list_of_hashes(service_buffer: bytes) -> List[Metadata.HashTag.Hash]:
    return calculate_hashes(
        value=service_buffer
    )
