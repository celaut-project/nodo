from typing import Union
import grpcbigbuffer as grpcbf

from src.utils.env import SHA3_256_ID, SHA3_256, SHAKE_256_ID, SHAKE_256, HASH_FUNCTIONS
from src.utils.logger import LOGGER

from protos.celaut_pb2 import Any, Service

def calculate_hashes(value) -> list:
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


def check_service(service_buffer: bytes, hashes: list) -> bool:
    for hash in hashes:
        if hash.type in HASH_FUNCTIONS and \
                hash.value == HASH_FUNCTIONS[hash.type](
            value=service_buffer
        ):
            return True
    return False


# Return the service's sha3-256 hash on hexadecimal format.
def get_service_hex_main_hash(
        service_buffer: Union[bytes, str, Service, tuple, None],
        partitions_model: tuple = None,
        metadata: Any.Metadata = Any.Metadata(),
        other_hashes: list = None
) -> str:
    # Find if it has the hash.
    if other_hashes is None:
        other_hashes = []
    for hash in list(metadata.hashtag.hash) + other_hashes:
        if hash.type == SHA3_256_ID:
            return hash.value.hex()

    # If not but the spec. is complete, calculate the hash pruning it before.
    # If not and is incomplete, it's going to be impossible calculate any hash.

    if not service_buffer:
        LOGGER(' sha3-256 hash function is not implemented on this method.')
        raise Exception(' sha3-256 hash function is not implemented on this method.')

    if type(service_buffer) is not tuple:
        try:
            return SHA3_256(
                value=service_buffer if type(service_buffer) is bytes
                else open(service_buffer, 'rb').read() if type(service_buffer) is str
                else Service(service_buffer).SerializeToString()
            ).hex()
        except Exception as e:
            LOGGER('Exception getting a service hash: ' + str(e))
            pass

    elif partitions_model:
        return SHA3_256(
            value=grpcbf.partitions_to_buffer(
                message_type=Service,
                partitions_model=partitions_model,
                partitions=service_buffer
            )
        ).hex()


def get_service_list_of_hashes(service_buffer: bytes, metadata: Any.Metadata, complete=True) -> list:
    if complete:
        return calculate_hashes(
            value=service_buffer
        )
    else:
        raise Exception("Can't get the hashes if the service is not complete.")
