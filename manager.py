from lib2to3.pgen2 import token
from wsgiref.simple_server import sys_version
import celaut_pb2
from iobigdata import IOBigData
import pymongo
import docker as docker_lib

db = pymongo.MongoClient(
            "mongodb://localhost:27017/"
        )["mongo"]["serviceInstances"]

# TODO get from enviroment variables.
DEFAULT_SYSTEM_PARAMETERS = celaut_pb2.Sysparams(
    mem_limit = 50*pow(10, 6),
)

system_cache = {} # token : { mem_limit : 0 }

def __push_token(token: str): 
    system_cache[token] = { "mem_limit": 0 }

def __pop_token(token: str): 
    del system_cache[token]
    __modify_sysreq(
        token = token,
        sym_req = celaut_pb2.Sysparams(
            mem_limit = 0
        )
    )

def manager_prevent():    # TODO Para comprobar que todas las cuentas sean correctas, se puede iterar en un hilo secundario.
    for token, sysreq in system_cache:
        if False: # If was killed.
            __pop_token(token = token)

def __modify_sysreq(token: str, sys_req: celaut_pb2.Sysparams) -> bool:
    if token not in system_cache.keys(): __push_token(token = token)
    if sys_req.HasField('mem_limit'):
        varriation = system_cache[token]['mem_limit'] - sys_req.mem_limit

        if varriation < 0:
            pass#IOBigData().lock_ram(ram_amount = (-1)* sys_req.mem_limit)

        elif varriation > 0:
            pass#IOBigData().unlock_ram(ram_amount = sys_req.mem_limit)

        system_cache[token]['mem_limit'] = sys_req.mem_limit

    return True

def container_modify_system_params(
            token: str, 
            system_requeriments: celaut_pb2.Sysparams = None
        ) -> bool:

    # https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.Container.update
    # Set system requeriments parameters.

    if not system_requeriments: system_requeriments = DEFAULT_SYSTEM_PARAMETERS

    if __modify_sysreq(
                token = token,
                sys_req = system_requeriments
            ):
        try:
            __get_cointainer_by_token(
                token = token
            ).update(
                    mem_limit = system_requeriments.mem_limit
                )
        except: return False
        return True

    return False 

def __get_cointainer_by_token(token: str) -> docker_lib.models.containers.Container:
    return docker_lib.from_env().containers.get(
        container_id = token.split('##')[-1]
    )


def could_ve_this_sysreq(sysreq: celaut_pb2.Sysparams) -> bool:
    return IOBigData().prevent_kill(len = sysreq.mem_limit)
    # It's not possible local, but other pair can, returns True.

def get_sysparams(token: str) -> celaut_pb2.Sysparams:
    return celaut_pb2.Sysparams(
        mem_limit = system_cache[token]["mem_limit"]
    )