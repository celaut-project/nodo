from lib2to3.pgen2 import token
from statistics import variance
from wsgiref.simple_server import sys_version
import celaut_pb2
from iobigdata import IOBigData
import pymongo
import docker as docker_lib
import logger as l
import gateway_pb2

db = pymongo.MongoClient(
            "mongodb://localhost:27017/"
        )["mongo"]["serviceInstances"]

# TODO get from enviroment variables.

DEFAULT_SYSTEM_RESOURCES = celaut_pb2.Sysresources(
    mem_limit = 50*pow(10, 6),
)

MEMSWAP_FACTOR = 0 # 0 - 1


# TODO system_cache_lock = Lock()

system_cache = {} # token : { mem_limit : 0 }

def __push_token(token: str): 
    system_cache[token] = { "mem_limit": 0 }

def __pop_token(token: str): 
    del system_cache[token]
    __modify_sysreq(
        token = token,
        sym_req = celaut_pb2.Sysresources(
            mem_limit = 0
        )
    )

def __modify_sysreq(token: str, sys_req: celaut_pb2.Sysresources) -> bool:
    if token not in system_cache.keys(): __push_token(token = token)
    if sys_req.HasField('mem_limit'):
        variation = system_cache[token]['mem_limit'] - sys_req.mem_limit

        if variation < 0:
            IOBigData().lock_ram(ram_amount = abs(variation))

        elif variation > 0:
            IOBigData().unlock_ram(ram_amount = variation)

        if variation != 0: system_cache[token]['mem_limit'] = sys_req.mem_limit

    return True

def __get_cointainer_by_token(token: str) -> docker_lib.models.containers.Container:
    return docker_lib.from_env().containers.get(
        container_id = token.split('##')[-1]
    )

def container_modify_system_params(
        token: str, 
        system_requeriments_range: gateway_pb2.ModifyServiceSystemResourcesInput = None
    ) -> bool:

    # https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.Container.update
    # Set system requeriments parameters.

    system_requeriments = system_requeriments_range.min_sysreq
    if not system_requeriments: return False

    if __modify_sysreq(
                token = token,
                sys_req = system_requeriments
            ):
        try:
            # Memory limit should be smaller than already set memoryswap limit, update the memoryswap at the same time
            __get_cointainer_by_token(
                token = token
            ).update(
                    mem_limit = system_requeriments.mem_limit if MEMSWAP_FACTOR == 0 \
                        else system_requeriments.mem_limit - MEMSWAP_FACTOR* system_requeriments.mem_limit,
                    memswap_limit = system_requeriments.mem_limit if MEMSWAP_FACTOR > 0 else -1
                )
        except: return False
        return True

    return False 

def container_stop(token: str) -> bool:
    if __modify_sysreq(
        token = token,
        sys_req = celaut_pb2.Sysresources(
            mem_limit = 0
        )
    ):
        del system_cache[token]
        return True

def could_ve_this_sysreq(sysreq: celaut_pb2.Sysresources) -> bool:
    return IOBigData().prevent_kill(len = sysreq.mem_limit) # Prevent kill dice de lo que dispone actualmente libre.
    # It's not possible local, but other pair can, returns True.

def get_sysresources(token: str) -> celaut_pb2.Sysresources:
    return celaut_pb2.Sysresources(
        mem_limit = system_cache[token]["mem_limit"]
    )

def manager_prevent():    # TODO Para comprobar que todas las cuentas sean correctas, se puede iterar en un hilo secundario.
    for token, sysreq in system_cache:
        if False: # If was killed.
            __pop_token(token = token)
        continue