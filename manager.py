from hashlib import sha256
import json
import string
from threading import Lock
import threading
from time import sleep
from typing import Dict
import build
import docker as docker_lib
from utils import GET_ENV, get_network_name, get_only_the_ip_from_context_method
import celaut_pb2
from iobigdata import IOBigData
import pymongo
import docker as docker_lib
import logger as l
from verify import get_service_hex_main_hash
import celaut_pb2 as celaut
import gateway_pb2_grpc,gateway_pb2, grpc, grpcbigbuffer as grpcbf
import contracts.vyper_gas_deposit_contract.interface as vyper_gdc
from google.protobuf.json_format import MessageToJson

db = pymongo.MongoClient(
            "mongodb://localhost:27017/"
        )["mongo"]["serviceInstances"]

# TODO get from enviroment variables.

DOCKER_CLIENT = lambda: docker_lib.from_env()
DEFAULT_SYSTEM_RESOURCES = celaut_pb2.Sysresources(
    mem_limit = 50*pow(10, 6),
)

DOCKER_NETWORK = 'docker0'
LOCAL_NETWORK = 'lo'

DEFAULT_INITIAL_GAS_AMOUNT_FACTOR = GET_ENV(env = 'DEFAULT_INITIAL_GAS_AMOUNT_FACTOR', default = 1/pow(10,6))  # Perdentage of the parent's gas amount.
DEFAULT_INTIAL_GAS_AMOUNT = GET_ENV(env = 'DEFAULT_INTIAL_GAS_AMOUNT', default = pow(10, 9)) # Only for services launched by the node.
COMPUTE_POWER_RATE = GET_ENV(env = 'COMPUTE_POWER_RATE', default = 2)
COST_OF_BUILD = GET_ENV(env = 'COST_OF_BUILD', default = 5)
EXECUTION_BENEFIT = GET_ENV(env = 'EXECUTION_BENEFIT', default = 1)
MANAGER_ITERATION_TIME = GET_ENV(env = 'MANAGER_ITERATION_TIME', default = 3)
MEMORY_LIMIT_COST_FACTOR = GET_ENV(env = 'MEMORY_LIMIT_COST_FACTOR', default = 1/pow(10,6))
MIN_PEER_DEPOSIT = GET_ENV(env = 'MIN_PEER_DEPOSIT', default = 10)
INITIAL_PEER_DEPOSIT_FACTOR = GET_ENV(env = 'INITIAL_PEER_DEPOSIT_FACTOR', default = 20)
COST_AVERAGE_VARIATION = GET_ENV(env = 'COST_AVERAGE_VARIATION', default=1)
GAS_COST_FACTOR = GET_ENV(env = 'GAS_COST_FACTOR', default = 1) # Applied only outside the manager. (not in maintain_cost)
MODIFY_SERVICE_SYSTEM_RESOURCES_COST = GET_ENV(env = 'MODIFY_SERVICE_SYSTEM_RESOURCES_COST_FACTOR', default = 1)
ALLOW_GAS_DEBT = GET_ENV(env = 'ALLOW_GAS_DEBT', default = True)  # Could be used with the reputation system.

PAYMENT_PROCESS_VALIDATORS: Dict[str, function] = {vyper_gdc.contract_hash : vyper_gdc.payment_process_validator}     # contract:  lambda peer_id, tx_id, amount -> bool,
AVAILABLE_PAYMENT_PROCESS: Dict[str, function] = {vyper_gdc.contract_hash : vyper_gdc.process_payment}   # contract:   lambda amount, peer_id -> tx_id,

MEMSWAP_FACTOR = 0 # 0 - 1


# CONTAINER CACHE

# Insert the instance if it does not exists.
def insert_instance_on_mongo(instance: celaut.Instance):
    parsed_instance = json.loads(MessageToJson(instance))
    pymongo.MongoClient(
        "mongodb://localhost:27017/"
    )["mongo"]["peerInstances"].update_one(
        filter = parsed_instance,
        update={'$setOnInsert': parsed_instance},
        upsert = True
    )

# SYSTEM CACHE

system_cache_lock = Lock()
system_cache = {} # token : { mem_limit: 0, gas: 0 }

peer_instances_lock = Lock()
peer_instances = {'192.168.1.13': pow(10, 16)} # id: amount_of_gas -> other peers' deposits on this node.

deposits_on_other_peers_lock = Lock()
deposits_on_other_peers = {}  # id: amount of gas -> the deposits in other peers.

container_cache_lock = threading.Lock()
container_cache = {}  # ip_father:[dependencies]

cache_service_perspective = {} # service_ip:(local_token or external_service_token)

# internal token -> str( peer_ip##container_ip##container_id )   peer_ip se refiere a la direccion del servicio padre (que puede ser interno o no).
# external token -> str( peer_ip##node_ip:node_port##his_token )

# En caso de mandarle la tarea a otro nodo:
#   En cache se añadirá el servicio como dependencia del nodo elegido,
#   en vez de usar la ip del servicio se pone el token que nos dió ese servicio,
#   nosotros a nuestro servicio solicitante le daremos un token con el formato node_ip##his_token.


def __set_on_cache( father_ip : str, container_id_or_external_token: str, local_or_external_token: str, ip_or_uri: str):

    # En caso de ser un nodo externo:
    if not father_ip in container_cache:
        container_cache_lock.acquire()
        container_cache.update({father_ip: []})
        container_cache_lock.release()
        # Si peer_ip es un servicio del nodo ya
        # debería estar en el registro.


    # Añade el nuevo servicio como dependencia.
    container_cache[father_ip].append(ip_or_uri + '##' + container_id_or_external_token)
    cache_service_perspective[ip_or_uri] = local_or_external_token
    l.LOGGER('Set on cache ' + ip_or_uri + '##' + container_id_or_external_token + ' as dependency of ' + father_ip )


def set_external_on_cache(father_ip : str, external_token: str, ip_or_uri: str):
    __set_on_cache(
        father_ip = father_ip,
        container_id_or_external_token = external_token,
        local_or_external_token = external_token,
        ip_or_uri = ip_or_uri
    )


def __purgue_internal(father_ip = None, container_id = None, container_ip = None, token = None) -> int:
    if token is None and (father_ip is None or container_id is None or container_ip is None):
        raise Exception('purgue_internal: token is None and (father_ip is None or container_id is None or container_ip is None)')

    try:
        DOCKER_CLIENT().containers.get(container_id).remove(force=True)
    except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
        l.LOGGER(str(e) + 'ERROR WITH DOCKER WHEN TRYING TO REMOVE THE CONTAINER ' + container_id)
        raise e

    refund = __get_gas_amount_by_ip(
            ip = container_ip
        )

    container_cache_lock.acquire()

    try:
        container_cache[father_ip].remove(container_ip + '##' + container_id)
    except ValueError as e:
        l.LOGGER(str(e) + str(container_cache[father_ip]) + ' trying to remove ' + container_ip + '##' + container_id)
    except KeyError as e:
        l.LOGGER(str(e) + father_ip + ' not in ' + str(container_cache.keys()))

    try:
        del cache_service_perspective[container_ip]
    except Exception as e:
        l.LOGGER('EXCEPTION NO CONTROLADA. ESTO NO DEBERÍA HABER OCURRIDO '+ str(e)+ ' ' + str(cache_service_perspective)+ ' ' + str(container_id))  # TODO. Study the imposibility of that.
        raise e

    with system_cache_lock: del system_cache[token]

    if container_ip in container_cache:
        for dependency in container_cache[container_ip]:
            # Si la dependencia esta en local.
            if get_network_name(ip_or_uri = dependency.split('##')[0]) == DOCKER_NETWORK:
                refund += __purgue_internal(
                    father_ip = container_ip,
                    container_id = dependency.split('##')[1],
                    container_ip = dependency.split('##')[0]
                )
            # Si la dependencia se encuentra en otro nodo.
            else:
                refund += __purgue_external(
                    father_ip = father_ip,
                    node_uri = dependency.split('##')[0],
                    token = dependency[len(dependency.split('##')[0]) + 1:] # Por si el token comienza en # ...
                )

        try:
            l.LOGGER('Deleting the instance ' + container_id + ' from cache with ' + str(container_cache[container_ip]) + ' dependencies.')
            del container_cache[container_ip]
        except KeyError as e:
            l.LOGGER(str(e) + container_ip + ' not in ' + str(container_cache.keys()))

    container_cache_lock.release()
    return refund


def __purgue_external(father_ip, node_uri, token) -> int:
    if len(node_uri.split(':')) < 2:
        l.LOGGER('Should be an uri not an ip. Something was wrong. The node uri is ' + node_uri)
        return None
    
    refund = 0
    container_cache_lock.acquire()

    try:
        container_cache[father_ip].remove(node_uri + '##' + token)
    except ValueError as e:
        l.LOGGER(str(e) + str(container_cache[father_ip]) + ' trying to remove ' + node_uri + '##' + token)
    except KeyError as e:
        l.LOGGER(str(e) + father_ip + ' not in ' + str(container_cache.keys()))

    # Le manda al otro nodo que elimine esa instancia.
    try:
        refund = next(grpcbf.client_grpc(
            method = gateway_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(
                            node_uri
                        )
                    ).StopService,
            input = gateway_pb2.TokenMessage(
                token = token
            ),
            output = gateway_pb2.Refund()
        )).amount
    except grpc.RpcError as e:
        l.LOGGER('Error during remove a container on ' + node_uri + ' ' + str(e))

    container_cache_lock.release()
    return refund


def get_token_by_uri(uri: str) -> str:
    try:
        return cache_service_perspective[uri]
    except Exception as e:
        l.LOGGER('EXCEPTION NO CONTROLADA. ESTO NO DEBERÍA HABER OCURRIDO '+ str(e)+ ' '+str(cache_service_perspective)+ ' '+str(uri))  # TODO. Study the imposibility of that.
        raise e

def __push_token(token: str): 
    with system_cache_lock: system_cache[token] = { "mem_limit": 0 }

def __modify_sysreq(token: str, sys_req: celaut_pb2.Sysresources) -> bool:
    if token not in system_cache.keys(): raise Exception('Manager error: token '+token+' does not exists.')
    if sys_req.HasField('mem_limit'):
        variation = system_cache[token]['mem_limit'] - sys_req.mem_limit

        if variation < 0:
            IOBigData().lock_ram(ram_amount = abs(variation))

        elif variation > 0:
            IOBigData().unlock_ram(ram_amount = variation)

        if variation != 0: 
            with system_cache_lock: system_cache[token]['mem_limit'] = sys_req.mem_limit

    return True

def __get_cointainer_by_token(token: str) -> docker_lib.models.containers.Container:
    return docker_lib.from_env().containers.get(
        container_id = token.split('##')[-1]
    )

def __refound_gas(
    gas: int,
    cache: dict,
    id: int
) -> bool: 
    try:
        cache[id] += gas
    except Exception as e:
        l.LOGGER('Manager error: '+str(e))
        return False
    return True

# Only can be executed once.
def __refound_gas_function_factory(
    gas: int,
    cache: dict,
    id: int,
    container: list = None
) -> lambda: None: 
    def use(l = [lambda: __refound_gas(gas, cache, id)]): l.pop()()
    if container: container.append( lambda: use() )


# Payment process for the manager.

def __peer_payment_process(peer_id: str, amount: int) -> bool:
    l.LOGGER('Peer payment process to '+peer_id+' by '+str(amount))
    for available_payment_process in AVAILABLE_PAYMENT_PROCESS.values():   # check if the payment process is compatible with this peer.
        try:            
            next(grpcbf.client_grpc(
                        method = gateway_pb2_grpc.GatewayStub(
                                    grpc.insecure_channel(
                                        peer_id+':8090', # TODO with port. Tiene que buscar en mongo, cuando se guarden por identificador.
                                    )
                                ).Payable,
                        partitions_message_mode_parser = True,
                        input = gateway_pb2.Payment(
                            gas_amount = amount,
                            deposit_token = peer_id,
                            contract_ledger = available_payment_process(
                                amount = amount, 
                                token = peer_id,
                            ),                            
                        )
                    )
                )
        except Exception as e:
            l.LOGGER('Peer payment process error: '+str(e))
            return False
        return True
    return False

def __increase_deposit_on_peer(peer_id: str, amount: int) -> bool:
    l.LOGGER('Increase deposit on peer '+peer_id+' by '+str(amount))
    if __peer_payment_process(peer_id = peer_id, amount = amount):  # process the payment on the peer.
        deposits_on_other_peers[peer_id] = deposits_on_other_peers[peer_id] + amount if peer_id in deposits_on_other_peers else amount
        return True
    return False

def __check_payment_process( amount: int, ledger: str, token: str, contract: bytes, contract_id: string) -> bool:
    l.LOGGER('Check payment process to '+token+' by '+str(amount))
    return PAYMENT_PROCESS_VALIDATORS[sha256(contract)]( amount, token, ledger, contract_id)

def __increase_local_gas_for_peer(peer_id: str, amount: int) -> bool:
    l.LOGGER('Increase local gas for peer '+peer_id+' by '+str(amount))
    if not __refound_gas(gas = amount, cache = peer_instances, id = peer_id):
        raise Exception('Manager error: cannot increase local gas for peer '+peer_id+' by '+str(amount))
    return True

def __get_gas_amount_by_ip(ip: str) -> int:
    l.LOGGER('Get gas amount for '+ip)
    if ip in peer_instances:
        return peer_instances[ip]
    elif ip in cache_service_perspective:
        return system_cache[
            get_token_by_uri(uri = ip)
        ]['gas']
    else:
        l.LOGGER('Manager error: cannot get gas amount for '+ip+' Caches -> '+str(cache_service_perspective) + str(system_cache) + str(peer_instances))
        raise Exception('Manager error: cannot get gas amount for '+ip)


def validate_payment_process(peer: str, amount: int, ledger: str, contract: bytes, contract_id: str, token: str) -> bool:
    return __check_payment_process(amount = amount, ledger = ledger, token = token, contract = contract, contract_id = contract_id) \
         and __increase_local_gas_for_peer(peer_id = get_only_the_ip_from_context_method(context_peer = peer), amount = amount)


def spend_gas(
    token_or_container_ip: str,   #  If its peer, the token is the peer id. 
                                  #  If its a local service, the token could be the token or the container ip, 
                                  #  on the last case, it take the token with cache service perspective.
    gas_to_spend: int,
    refund_gas_function_container: list = None
) -> bool:
    gas_to_spend = int(gas_to_spend)
    l.LOGGER('Spend '+str(gas_to_spend)+' gas by ' + token_or_container_ip)
    try:
        if token_or_container_ip in peer_instances and (peer_instances[token_or_container_ip] >= gas_to_spend or ALLOW_GAS_DEBT):
            l.LOGGER( str(gas_to_spend)+' of '+str(peer_instances[token_or_container_ip]))
            peer_instances[token_or_container_ip] -= gas_to_spend
            __refound_gas_function_factory(
                gas = gas_to_spend, 
                cache = peer_instances, 
                id = token_or_container_ip, 
                container = refund_gas_function_container
            )
            return True
        elif token_or_container_ip in system_cache and (system_cache[token_or_container_ip]['gas'] >= gas_to_spend or ALLOW_GAS_DEBT):
            l.LOGGER( str(gas_to_spend)+' of '+str(system_cache[token_or_container_ip]['gas']))
            with system_cache_lock: system_cache[token_or_container_ip]['gas'] -= gas_to_spend
            __refound_gas_function_factory(
                gas = gas_to_spend, 
                cache = peer_instances, 
                id = token_or_container_ip, 
                container = refund_gas_function_container
            )
            return True
        
        token_or_container_ip = cache_service_perspective[token_or_container_ip]
        if token_or_container_ip in system_cache and (system_cache[token_or_container_ip]['gas'] >= gas_to_spend or ALLOW_GAS_DEBT):
            l.LOGGER( str(gas_to_spend)+' of '+str(system_cache[token_or_container_ip]['gas']))
            with system_cache_lock: system_cache[token_or_container_ip]['gas'] -= gas_to_spend
            __refound_gas_function_factory(
                gas = gas_to_spend, 
                cache = peer_instances, 
                id = token_or_container_ip, 
                container = refund_gas_function_container
            )
            return True
    except Exception as e: l.LOGGER('Manager error '+str(e))
    
    l.LOGGER(token_or_container_ip+" can't spend "+str(gas_to_spend)+" gas. \n Status the list of avaliable gas: "+str(peer_instances)+ " and "+str(system_cache))
    return False


def add_peer(
    peer_id: str
) -> bool:
    l.LOGGER('Add peer '+ peer_id)

    if peer_id not in peer_instances:
        peer_instances[peer_id] = 0
        return __increase_local_gas_for_peer(peer_id = peer_id, amount = INITIAL_PEER_DEPOSIT_FACTOR * MIN_PEER_DEPOSIT)
    return False


def default_cost(
    father_ip: str = None
) -> int:
    l.LOGGER('Default cost for '+(father_ip if father_ip else 'local'))
    return ( int( __get_gas_amount_by_ip( ip = father_ip ) * DEFAULT_INITIAL_GAS_AMOUNT_FACTOR ) ) if father_ip else int(DEFAULT_INTIAL_GAS_AMOUNT)

def add_container(
    father_ip: str,
    container: docker_lib.models.containers.Container,
    initial_gas_amount: int,
    system_requeriments_range: gateway_pb2.ModifyServiceSystemResourcesInput = None
) -> str:
    l.LOGGER('Add container for '+ father_ip)
    token = father_ip + '##' + container.attrs['NetworkSettings']['IPAddress'] + '##' + container.id
    if token in system_cache.keys(): raise Exception('Manager error: '+token+' exists.')

    __push_token(token = token)
    __set_on_cache(
        father_ip = father_ip,
        container_id_or_external_token = container.id,
        ip_or_uri = container.attrs['NetworkSettings']['IPAddress'],
        local_or_external_token = token,
    )
    with system_cache_lock: system_cache[token]['gas'] = initial_gas_amount if initial_gas_amount else default_cost(father_ip = father_ip)
    if not container_modify_system_params(
        token = token,
        system_requeriments_range = system_requeriments_range
    ): raise Exception('Manager error adding '+token+'.')
    return token

def container_modify_system_params(
        token: str, 
        system_requeriments_range: gateway_pb2.ModifyServiceSystemResourcesInput = None
    ) -> bool:

    l.LOGGER('Modify params of '+ token)

    # https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.Container.update
    # Set system requeriments parameters.

    system_requeriments = system_requeriments_range.max_sysreq  # TODO implement the use of min_sysreq.
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


def could_ve_this_sysreq(sysreq: celaut_pb2.Sysresources) -> bool:
    return IOBigData().prevent_kill(len = sysreq.mem_limit) # Prevent kill dice de lo que dispone actualmente libre.
    # It's not possible local, but other pair can, returns True.

def get_sysresources(token: str) -> celaut_pb2.Sysresources:
    return celaut_pb2.Sysresources(
        mem_limit = system_cache[token]["mem_limit"]
    )


# PRUNE CONTAINER METHOD

def prune_container(token: str) -> int:
    l.LOGGER('Prune container '+ token)
    if get_network_name(ip_or_uri = token.split('##')[1]) == DOCKER_NETWORK: # Suponemos que no tenemos un token externo que empieza por una direccion de nuestra subnet.
        try:
            refund = __purgue_internal(
                father_ip = token.split('##')[0],
                container_id = token.split('##')[2],
                container_ip = token.split('##')[1],
                token = token
            )
        except Exception as e:
            l.LOGGER('Error purging '+token+' '+str(e))
            return False
        
    else:
        try:
            refund = __purgue_external(
                father_ip = token.split('##')[0],
                node_uri = token.split('##')[1],
                token = token[len( token.split('##')[1] ) + 1:] # Por si el token comienza en # ...
            )
        except Exception as e:
            l.LOGGER('Error purging '+token+' '+str(e))
            return False
    
    __increase_deposit_on_peer(
        peer_id = token.split('##')[0],
        amount = refund
    )
    return refund



# COST FUNCTIONS

def maintain_cost(sysreq: dict) -> int:
    return MEMORY_LIMIT_COST_FACTOR * sysreq['mem_limit']

def build_cost(service_buffer: bytes, metadata: celaut.Any.Metadata) -> int:
    is_built = (get_service_hex_main_hash(service_buffer = service_buffer, metadata = metadata) \
                    in [img.tags[0].split('.')[0] for img in DOCKER_CLIENT().images.list()])
    if not is_built and \
        not build.check_supported_architecture(metadata=metadata): raise build.UnsupportedArquitectureException
    try:
        # Coste de construcción si no se posee el contenedor del servicio.
        # Debe de tener en cuenta el coste de buscar el conedor por la red.
        return sum([
                COST_OF_BUILD * (is_built is False),
                # Coste de obtener el contenedor ... #TODO
            ])
    except:
        pass
    return COST_OF_BUILD

def execution_cost(service_buffer: bytes, metadata: celaut.Any.Metadata) -> int:
    l.LOGGER('Get execution cost')
    try:
        return sum([
            len( DOCKER_CLIENT().containers.list() )* COMPUTE_POWER_RATE,
            build_cost(service_buffer = service_buffer, metadata = metadata),
            EXECUTION_BENEFIT
        ]) 
    except build.UnsupportedArquitectureException as e: raise e
    except Exception as e:
        l.LOGGER('Error calculating execution cost '+str(e))
        raise e

def start_service_cost(
    metadata,
    service_buffer,
    initial_gas_amount: int
) -> int:
    return execution_cost(
        service_buffer = service_buffer,
        metadata = metadata
    ) + initial_gas_amount


# THREAD

def maintain():
    l.LOGGER('Maintain '+str(system_cache))
    for token, sysreq in dict(system_cache).items():  # Parse the system cache to other dict to avoid concurrent access.
        try:
            if DOCKER_CLIENT().containers.get(token.split('##')[-1]).status == 'exited':
                prune_container(token = token)
        except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
            l.LOGGER(str(e) + 'ERROR WITH DOCKER WHEN TRYING TO GET THE CONTAINER ' + token)
        
        if not spend_gas(
            token_or_container_ip = token,
            gas_to_spend = maintain_cost(sysreq)
        ):
            try:
                prune_container(token=token)
            except Exception as e:
                l.LOGGER('Error purging '+token+' '+str(e))
                raise Exception('Error purging '+token+' '+str(e))


def pair_deposits():
    for peer, deposit in deposits_on_other_peers.items():
        if deposit < MIN_PEER_DEPOSIT:
            l.LOGGER('Manager error: the peer '+ str(peer)+' has not enough deposit.')
            if not __increase_deposit_on_peer(peer_id = peer):
                l.LOGGER('Manager error: the peer '+ str(peer)+' could not be increased.')
                del deposits_on_other_peers[peer]


def manager_thread():
    while True:
        maintain()
        pair_deposits()
        sleep(MANAGER_ITERATION_TIME) 