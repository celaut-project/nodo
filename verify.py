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
    # Needs to check all fields.
    return service.container.filesystem.HasField('branch')

def get_service_hex_hash(service: Service) -> str:
    if is_complete_service(service=service):
        return SHA3_256(
            value = prune_hashes_of_service(
                service=service
            ).SerializeToString()
        ).hex()
    elif SHA3_256_ID in service.hashtag.hash:
        return service.hashtag.hash[SHA3_256_ID].hex()
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
