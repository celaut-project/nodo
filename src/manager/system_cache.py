import time
from threading import Lock

import docker as docker_lib
import grpc
from grpcbigbuffer import client as grpcbf

from protos import gateway_pb2_grpc, gateway_pb2
from src.utils import logger as l
from src.utils.env import CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME, CLIENT_EXPIRATION_TIME, DOCKER_CLIENT, \
    DOCKER_NETWORK, REMOVE_CONTAINERS
from src.utils.singleton import Singleton
from src.utils.utils import get_network_name, from_gas_amount, generate_uris_by_peer_id


class LockCaches:
    def __init__(self):
        self.d = {}  # key, token

    def lock(self, token: str):
        if token not in self.d:
            self.d[token] = CacheLock()

        return self.d[token]

    def delete(self, token: str):
        del self.d[token]


class CacheLock:
    def __init__(self):
        self.lock = Lock()

    def __enter__(self):
        self.lock.__enter__()

    def __exit__(self, exception_type, exception_value, traceback):
        self.lock.__exit__()


class Client:

    def __init__(self, gas: int = 0):
        self.gas: int = gas
        self.last_usage: float = None

    def add_gas(self, gas: int):
        self.gas += gas
        if self.last_usage and self.gas >= CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME: self.last_usage = None

    def reduce_gas(self, gas: int):
        self.gas -= gas
        if self.gas == 0 and self.last_usage: self.last_usage = time.time()

    def is_expired(self) -> bool:
        return self.last_usage and ((time.time() - self.last_usage) >= CLIENT_EXPIRATION_TIME)


class SystemCache(metaclass=Singleton):
    cache_locks = LockCaches()

    system_cache = {}  # token : { mem_limit: 0, gas: 0 }

    clients = {
        'dev': Client(
            gas=pow(10, 128)
        )
    }  # client_id: amount_of_gas -> deposits on this node.

    total_deposited_on_other_peers = {}  # client_id: amount of gas -> the deposits in other peers.

    clients_on_other_peers = {}  # peer_id : client_id

    container_cache_lock = Lock()
    container_cache = {}  # ip_father:[dependencies]

    # Lock not needed.
    cache_service_perspective = {}  # service_ip:(local_token or external_service_token)

    # Lock not needed.
    external_token_hash_map = {}  # sha256( token ) : token

    # internal token -> str( peer_ip##container_ip##container_id )   peer_ip se refiere a la direccion del servicio padre (que puede ser interno o no).
    # external token -> str( peer_ip##node_ip:node_port##his_token )

    # En caso de mandarle la tarea a otro nodo:
    #   En cache se añadirá el servicio como dependencia del nodo elegido,
    #   en vez de usar la ip del servicio se pone el token que nos dió ese servicio,
    #   nosotros a nuestro servicio solicitante le daremos un token con el formato node_ip##his_token.

    def set_on_cache(self,
                     agent_id: str,
                     container_ip___peer_id: str,
                     container_id___his_token_encrypt: str,
                     container_id____his_token: str
                     ):
        # En caso de ser un nodo externo:
        if not agent_id in self.container_cache:
            self.container_cache_lock.acquire()
            self.container_cache.update({agent_id: []})
            self.container_cache_lock.release()
            # Si peer_ip es un servicio del nodo ya
            # debería estar en el registro.

        # Añade el nuevo servicio como dependencia.
        self.container_cache[agent_id].append(container_ip___peer_id + '##' + container_id___his_token_encrypt)
        self.cache_service_perspective[container_ip___peer_id] = container_id____his_token
        l.LOGGER(
            'Set on cache ' + container_ip___peer_id + '##' + container_id___his_token_encrypt + ' as dependency of ' + agent_id)

    def set_external_on_cache(self, agent_id: str, encrypted_external_token: str, external_token: str, peer_id: str):
        self.external_token_hash_map[encrypted_external_token] = external_token
        self.set_on_cache(
            agent_id=agent_id,
            container_id___his_token_encrypt=encrypted_external_token,
            container_id____his_token=external_token,
            container_ip___peer_id=peer_id
        )

    def get_token_by_uri(self, uri: str) -> str:
        try:
            return self.cache_service_perspective[uri]
        except Exception as e:
            l.LOGGER('EXCEPTION NO CONTROLADA. ESTO NO DEBERÍA HABER OCURRIDO ' + str(e) + ' ' + str(
                self.cache_service_perspective) + ' ' + str(uri))  # TODO. Study the imposibility of that.
            raise e


    def get_gas_amount_by_id(self, id: str) -> int:
        l.LOGGER('Get gas amount for ' + id)

        if id in self.cache_service_perspective:
            return self.system_cache[
                self.get_token_by_uri(uri=id)
            ]['gas']

        if id in self.clients:
            return self.clients[id].gas

        raise Exception('Manager error: ' + id + ' not found.')

    def __get_gas_amount_by_ip(self, ip: str) -> int:
        return self.get_gas_amount_by_id(id=ip)

    def purgue_internal(self, agent_id=None, container_id=None, container_ip=None, token=None) -> int:
        if token is None and (agent_id is None or container_id is None or container_ip is None):
            raise Exception(
                'purgue_internal: token is None and (father_ip is None or container_id is None or container_ip is None)')

        if REMOVE_CONTAINERS:
            try:
                DOCKER_CLIENT().containers.get(container_id).remove(force=True)
            except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
                l.LOGGER(str(e) + 'ERROR WITH DOCKER WHEN TRYING TO REMOVE THE CONTAINER ' + container_id)
                return 0

        refund = self.__get_gas_amount_by_ip(
            ip=container_ip
        )

        with self.container_cache_lock:
            try:
                self.container_cache[agent_id].remove(container_ip + '##' + container_id)
            except ValueError as e:
                l.LOGGER(
                    str(e) + str(self.container_cache[agent_id]) + ' trying to remove ' + container_ip + '##' + container_id)
            except KeyError as e:
                l.LOGGER(str(e) + agent_id + ' not in ' + str(self.container_cache.keys()))

            try:
                del self.cache_service_perspective[container_ip]
            except Exception as e:
                l.LOGGER('EXCEPTION NO CONTROLADA. ESTO NO DEBERÍA HABER OCURRIDO ' + str(e) + ' ' + str(
                    self.cache_service_perspective) + ' ' + str(container_id))  # TODO. Study the imposibility of that.
                raise e

            del self.system_cache[token]
            self.cache_locks.delete(token)

            if container_ip in self.container_cache:
                for dependency in self.container_cache[container_ip]:
                    # Si la dependencia esta en local.
                    if get_network_name(ip_or_uri=dependency.split('##')[0]) == DOCKER_NETWORK:
                        refund += self.purgue_internal(
                            agent_id=container_ip,
                            container_id=dependency.split('##')[1],
                            container_ip=dependency.split('##')[0]
                        )
                    # Si la dependencia se encuentra en otro nodo.
                    else:
                        refund += self.purgue_external(
                            agent_id=agent_id,
                            peer_id=dependency.split('##')[0],
                            his_token=dependency[len(dependency.split('##')[0]) + 1:]
                            # Por si el token comienza en # ...
                        )

                try:
                    l.LOGGER('Deleting the instance ' + container_id + ' from cache with ' + str(
                        self.container_cache[container_ip]) + ' dependencies.')
                    del self.container_cache[container_ip]
                except KeyError as e:
                    l.LOGGER(str(e) + container_ip + ' not in ' + str(self.container_cache.keys()))

        return refund

    def purgue_external(self, agent_id, peer_id, his_token) -> int:
        refund = 0
        self.container_cache_lock.acquire()

        try:
            self.container_cache[agent_id].remove(peer_id + '##' + his_token)
        except ValueError as e:
            l.LOGGER(str(e) + '. Container cache of ' + agent_id + str(
                self.container_cache[agent_id]) + ' trying to remove ' + peer_id + '##' + his_token)
        except KeyError as e:
            l.LOGGER(str(e) + agent_id + ' not in ' + str(self.container_cache.keys()))

        # Le manda al otro nodo que elimine esa instancia.
        try:
            refund = from_gas_amount(next(grpcbf.client_grpc(
                method=gateway_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(
                        next(generate_uris_by_peer_id(peer_id=peer_id))
                    )
                ).StopService,
                input=gateway_pb2.TokenMessage(
                    token=self.external_token_hash_map[his_token]
                ),
                indices_parser=gateway_pb2.Refund,
                partitions_message_mode_parser=True
            )).amount)
        except grpc.RpcError as e:
            l.LOGGER('Error during remove a container on ' + peer_id + ' ' + str(e))

        self.container_cache_lock.release()
        return refund
