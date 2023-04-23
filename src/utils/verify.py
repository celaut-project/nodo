import hashlib
from typing import Union, Generator, List
from grpcbigbuffer import client as grpcbf

from src.utils.env import SHA3_256_ID, SHA3_256, SHAKE_256_ID, SHAKE_256, HASH_FUNCTIONS
from src.utils.logger import LOGGER

from protos.celaut_pb2 import Any, Service


def calculate_hashes_by_stream(value: Generator[bytes, None, None]) -> List[Any.Metadata.HashTag.Hash]:
    sha3_256 = hashlib.sha3_256()
    shake_256 = hashlib.shake_256()
    for chunk in value:
        sha3_256.update(chunk)
        shake_256.update(chunk)

    return [
        Any.Metadata.HashTag.Hash(
            type=SHA3_256_ID,
            value=sha3_256.digest()
        ),
        Any.Metadata.HashTag.Hash(
            type=SHAKE_256_ID,
            value=shake_256.digest(32)
        )
    ]


def calculate_hashes(value: bytes) -> List[Any.Metadata.HashTag.Hash]:
    return [
        Any.Metadata.HashTag.Hash(
            type=SHA3_256_ID,
            value=SHA3_256(value)
        ),
        Any.Metadata.HashTag.Hash(
            type=SHAKE_256_ID,
            value=SHAKE_256(value)
        )
    ]


# Return the service's sha3-256 hash on hexadecimal format.
def get_service_hex_main_hash(
        metadata: Any.Metadata = None,
        other_hashes: list = None
) -> str:
    # Find if it has the hash.
    if other_hashes is None:
        other_hashes = []
    if metadata is None:
        metadata = Any.Metadata()

    for hash in list(metadata.hashtag.hash) + other_hashes:
        if hash.type == SHA3_256_ID:
            return hash.value.hex()


def get_service_list_of_hashes(service_buffer: bytes) -> list:
    return calculate_hashes(
        value=service_buffer
    )
