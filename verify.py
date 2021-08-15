from grpc import ServiceRpcHandler
from logger import LOGGER
import hashlib
from ipss_pb2 import Service, HashTag
from google.protobuf.internal.containers import RepeatedCompositeFieldContainer

# -- HASH IDs --
SHAKE_256_ID = bytes.fromhex("46b9dd2b0ba88d13233b3feb743eeb243fcd52ea62b81b82b50c27646ed5762f")
SHA3_256_ID = bytes.fromhex("a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a")

# -- HASH FUNCTIONS --
SHAKE_256 = lambda value: "" if value is None else hashlib.shake_256(value).digest(32)
SHA3_256 = lambda value: "" if value is None else hashlib.sha3_256(value).digest()

def calculate_hashes(value) -> list:
    return [
        HashTag.Hash(
            type = SHA3_256_ID, 
            value = SHA3_256(value)
        ),
        HashTag.Hash(
            type = SHAKE_256_ID,
            value = SHAKE_256(value)
        )
    ]


def prune_hashes_of_service(service: Service) -> Service:
    def recursive_prune(field: any) -> any:
        try:
            for attribute in field.ListFields():
                if attribute[0].number == 15: # The hash field is always on index 15.
                    field.ClearField(attribute[0].name) # 'name' is the field's name on our serializer.
                else:
                    recursive_prune(field=attribute[1])
        except AttributeError:
            if type(field) == RepeatedCompositeFieldContainer:
                for elem in field:
                    recursive_prune(field=elem)

    s = Service()
    s.CopyFrom(service)
    recursive_prune(field=s)
    return s

def is_complete_service(service: Service) -> bool:
    # Needs to check all fields. But this serves at the moment.
    return len(service.container.filesystem.branch) > 0

# Return the service's sha3-256 hash on hexadecimal format.
def get_service_hex_hash(service: Service) -> str:
    # Find if it has the hash.
    for hash in  service.hashtag.hash:
        if hash.type == SHA3_256_ID:
            return hash.value.hex()

    # If not but the spec. is complete, calculate the hash prunning it before.
    # If not and is incomplete, it's going to be imposible calculate any hash.
    if is_complete_service(service=service):
        return SHA3_256(
            value = prune_hashes_of_service(
                service=service
            ).SerializeToString()
        ).hex()
    else:
        LOGGER(' sha3-256 hash function is not implemented on this method.')
        raise Exception(' sha3-256 hash function is not implemented on this method.')

def get_service_list_of_hashes(service: Service) -> list:
    if is_complete_service(service=service):
        return calculate_hashes(
            value = prune_hashes_of_service(
                service=service
            ).SerializeToString()
        )
    else:
        raise Exception("Can't get the hashes if the service is not complete.")
