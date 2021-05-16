import hashlib
from ipss_pb2 import Service
from google.protobuf.internal.containers import RepeatedCompositeFieldContainer

# -- HASH FUNCTIONS --
SHAKE_256 = lambda value: "" if value is None else 'shake-256:'+hashlib.shake_256(value).hexdigest(32)
SHA3_256 = lambda value: "" if value is None else 'sha3-256:'+hashlib.sha3_256(value).hexdigest()

def calculate_hashes(value) -> list:
    return [
        SHA3_256(value),
        SHAKE_256(value)
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

def get_service_hash(service: Service, hash_type: str) -> str:
    for hash in service.hash:
        if hash_type == hash.split(':')[0]:
            return hash.split(':')[1]
    
    # if not is_complete_service(service=service): return ''
    from compile import LOGGER
    if hash_type == "sha3-256":
        return SHA3_256(
            value = prune_hashes_of_service(
                service=service
            ).SerializeToString()
        ).split(':')[1]
    else:
        LOGGER(hash_type+' hash function is not implemented on this method.')
        return ''

def get_service_list_of_hashes(service: Service) -> list:
    # if not is_complete_service(service=service): return []
    return calculate_hashes(
        value = prune_hashes_of_service(
            service=service
        ).SerializeToString()
    )
