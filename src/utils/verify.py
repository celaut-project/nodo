from typing import Generator, List

from src.utils.hashing import (
    BLAKE2B_ID,
    HASH_SPECS,
    SHA3_256_ID,
    SHAKE_256_ID,
    get_configured_hash_id,
    hash_stream_many,
)
from protos.celaut_pb2 import Metadata

# Every algorithm a packed service carries a digest for, regardless of which one
# is configured for its main id (hashing.HASH). SHA3_256 and SHAKE_256 are the
# two the format has always shipped; BLAKE2B joins them so a service is
# verifiable under it too without a repack, should hashing.HASH ever be set to
# it on some node.
_RECORDED_HASH_IDS = (SHA3_256_ID, SHAKE_256_ID, BLAKE2B_ID)


def calculate_hashes_by_stream(value: Generator[bytes, None, None]) -> List[Metadata.HashTag.Hash]:
    digests = hash_stream_many(
        value, [HASH_SPECS[hash_id] for hash_id in _RECORDED_HASH_IDS]
    )
    return [
        Metadata.HashTag.Hash(type=hash_id, value=digests[hash_id])
        for hash_id in _RECORDED_HASH_IDS
    ]


def calculate_hashes(value: bytes) -> List[Metadata.HashTag.Hash]:
    return calculate_hashes_by_stream([value])


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
