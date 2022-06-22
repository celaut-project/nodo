from threading import Lock
from time import sleep
import build
import docker as docker_lib
from utils import GET_ENV
import celaut_pb2
from iobigdata import IOBigData
import pymongo
import docker as docker_lib
import logger as l
from verify import get_service_hex_main_hash
import celaut_pb2 as celaut
import gateway_pb2_grpc,gateway_pb2, grpc, grpcbigbuffer as grpcbf
import contracts.vyper_gas_deposit_contract as vyper_gdc

db = pymongo.MongoClient(
            "mongodb://localhost:27017/"
        )["mongo"]["serviceInstances"]

# TODO get from enviroment variables.

DOCKER_CLIENT = lambda: docker_lib.from_env()
DEFAULT_SYSTEM_RESOURCES = celaut_pb2.Sysresources(
    mem_limit = 50*pow(10, 6),
)

DEFAULT_INITIAL_GAS_AMOUNT = GET_ENV(env = 'DEFAULT_INITIAL_GAS_AMOUNT', default = 500)
COMPUTE_POWER_RATE = GET_ENV(env = 'COMPUTE_POWER_RATE', default = 2)
COST_OF_BUILD = GET_ENV(env = 'COST_OF_BUILD', default = 5)
EXECUTION_BENEFIT = GET_ENV(env = 'EXECUTION_BENEFIT', default = 1)
MANAGER_ITERATION_TIME = GET_ENV(env = 'MANAGER_ITERATION_TIME', default = 3)
MEMORY_LIMIT_COST_FACTOR = GET_ENV(env = 'MEMORY_LIMIT_COST_FACTOR', default = 0.01)
MIN_PEER_DEPOSIT = GET_ENV(env = 'MIN_PEER_DEPOSIT', default = 10)
INITIAL_PEER_DEPOSIT_FACTOR = GET_ENV(env = 'INITIAL_PEER_DEPOSIT_FACTOR', default = 2)
COST_AVERAGE_VARIATION = GET_ENV(env = 'COST_AVERAGE_VARIATION', default=1)
GAS_COST_FACTOR = GET_ENV(env = 'GAS_COST_FACTOR', default = 1)
MODIFY_SERVICE_SYSTEM_RESOURCES_COST_FACTOR = GET_ENV(env = 'MODIFY_SERVICE_SYSTEM_RESOURCES_COST_FACTOR', default = 1)

PAYMENT_PROCESS_VALIDATORS = {'VYPER': vyper_gdc.payment_process_validator}     # ledger_id:  lambda peer_id, tx_id, amount -> bool,
AVALIABLE_PAYMENT_PROCESS = {'VYPER': vyper_gdc.process_payment}   #ledger_id:   lambda amount, peer_id -> tx_id,

MEMSWAP_FACTOR = 0 # 0 - 1


system_cache_lock = Lock()
system_cache = {} # token : { mem_limit: 0, gas: 0 }

peer_instances_lock = Lock()
peer_instances = {'192.168.1.13': 999999} # id: amount_of_gas -> other peers' deposits on this node.

deposits_on_other_peers_lock = Lock()
deposits_on_other_peers = {}  # id: amount of gas -> the deposits in other peers.

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
    for payment_ledger, avaliable_payment_process in AVALIABLE_PAYMENT_PROCESS.items(): # check if the payment process is compatible with this peer.
        try:            
            next(grpcbf.client_grpc(
                        method = gateway_pb2_grpc.GatewayStub(
                                    grpc.insecure_channel(
                                        peer_id+':8090', # TODO with port. Tiene que buscar en mongo, cuando se guarden por identificador.
                                    )
                                ).Payment,
                        partitions_message_mode_parser = True,
                        input = gateway_pb2.Payment(
                            amount = amount,
                            tx_id = avaliable_payment_process(amount = amount, peer_id = peer_id),
                            ledger = payment_ledger,                            
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

def __check_payment_process(peer_id: str, amount: int, tx_id: str, ledger: str) -> bool:
    l.LOGGER('Check payment process to '+peer_id+' by '+str(amount))
    return PAYMENT_PROCESS_VALIDATORS[ledger](peer_id, amount, tx_id)

def __increase_local_gas_for_peer(peer_id: str, amount: int) -> bool:
    l.LOGGER('Increase local gas for peer '+peer_id+' by '+str(amount))
    if not __refound_gas(gas = amount, cache = peer_instances, id = peer_id):
        raise Exception('Manager error: cannot increase local gas for peer '+peer_id+' by '+str(amount))
    return True

def validate_payment_process(peer: str, amount: int, tx_id: str, ledger: str) -> bool:
    return __check_payment_process(peer = peer, amount = amount, tx_id = tx_id, ledger = ledger) \
         and __increase_local_gas_for_peer(peer_id = peer, amount = amount)

def spend_gas(
    id: str,
    gas_to_spend: int,
    refund_gas_function_container: list = None
) -> bool:
    l.LOGGER('Spend '+str(gas_to_spend)+' gas by ' + id)
    try:
        if id in peer_instances and peer_instances[id] >= gas_to_spend:
            l.LOGGER( str(gas_to_spend)+' of '+str(peer_instances[id]))
            peer_instances[id] -= gas_to_spend
            __refound_gas_function_factory(
                gas = gas_to_spend, 
                cache = peer_instances, 
                id = id, 
                container = refund_gas_function_container
            )
            return True
        elif id in system_cache and system_cache[id]['gas'] >= gas_to_spend:
            l.LOGGER( str(gas_to_spend)+' of '+str(system_cache[id]['gas']))
            with system_cache_lock: system_cache[id]['gas'] -= gas_to_spend
            __refound_gas_function_factory(
                gas = gas_to_spend, 
                cache = peer_instances, 
                id = id, 
                container = refund_gas_function_container
            )
            return True
    except Exception as e: l.LOGGER('Manager error '+str(e))
    
    l.LOGGER(id+" can't spend "+str(gas_to_spend)+" gas.")
    return False


def add_peer(
    peer_id: str
) -> bool:
    l.LOGGER('Add peer '+ peer_id)

    if peer_id not in peer_instances:
        peer_instances[peer_id] = 0
        return __increase_local_gas_for_peer(peer_id, INITIAL_PEER_DEPOSIT_FACTOR * MIN_PEER_DEPOSIT)
    return False


def add_container(
    father_ip: str,
    container: docker_lib.models.containers.Container,
    initial_gas_amount: int = DEFAULT_INITIAL_GAS_AMOUNT,
    system_requeriments_range: gateway_pb2.ModifyServiceSystemResourcesInput = None
) -> str:
    l.LOGGER('Add container for '+ father_ip)
    token = father_ip + '##' + container.attrs['NetworkSettings']['IPAddress'] + '##' + container.id
    if token in system_cache.keys(): raise Exception('Manager error: '+token+' exists.')

    __push_token(token = token)
    with system_cache_lock: system_cache[token]['gas'] = initial_gas_amount if initial_gas_amount else DEFAULT_INITIAL_GAS_AMOUNT
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

def pop_container_on_cache(token: str) -> bool:
    if __modify_sysreq(
        token = token,
        sys_req = celaut_pb2.Sysresources(
            mem_limit = 0
        )
    ):
        with system_cache_lock: del system_cache[token]
        return True
    return False

def could_ve_this_sysreq(sysreq: celaut_pb2.Sysresources) -> bool:
    return IOBigData().prevent_kill(len = sysreq.mem_limit) # Prevent kill dice de lo que dispone actualmente libre.
    # It's not possible local, but other pair can, returns True.

def get_sysresources(token: str) -> celaut_pb2.Sysresources:
    return celaut_pb2.Sysresources(
        mem_limit = system_cache[token]["mem_limit"]
    )


# Cost functions

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
    try:
        return sum([
            len( DOCKER_CLIENT().containers.list() )* COMPUTE_POWER_RATE,
            build_cost(service_buffer = service_buffer, metadata = metadata),
            EXECUTION_BENEFIT
        ]) 
    except build.UnsupportedArquitectureException as e: raise e

def start_service_cost(
    metadata,
    service_buffer,
    initial_gas_amount: int = DEFAULT_INITIAL_GAS_AMOUNT
) -> int:
    return execution_cost(
        service_buffer = service_buffer,
        metadata = metadata
    ) + initial_gas_amount


# Thread

def maintain():
    l.LOGGER('Maintain '+str(system_cache))
    for token, sysreq in dict(system_cache).items():  # Parse the system cache to other dict to avoid concurrent access.
        try:
            if DOCKER_CLIENT().containers.get(token.split('##')[-1]).status == 'exited':
                if not pop_container_on_cache(token = token):
                    l.LOGGER('Manager error: the service '+ token+' could not be stopped.')
        except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
            l.LOGGER(str(e) + 'ERROR WITH DOCKER WHEN TRYING TO GET THE CONTAINER ' + token)
        
        if not spend_gas(
            id = token,
            gas_to_spend = maintain_cost(sysreq)
        ) and not pop_container_on_cache(
                    token = token
                ): raise Exception('Manager error: the service '+ token+' could not be stopped.')

def pair_deposits():
    for peer, deposit in deposits_on_other_peers.items():
        if deposit < MIN_PEER_DEPOSIT:
            l.LOGGER('Manager error: the peer '+ str(peer)+' has not enough deposit.')
            if not __increase_deposit_on_peer(peer = peer):
                l.LOGGER('Manager error: the peer '+ str(peer)+' could not be increased.')
                del deposits_on_other_peers[peer]


def manager_thread():
    while True:
        maintain()
        pair_deposits()
        sleep(MANAGER_ITERATION_TIME) 