import os
import sqlite3
import time
from threading import Lock
from typing import List, Tuple, TypedDict

import docker as docker_lib
import grpc
from grpcbigbuffer import client as grpcbf

from protos import gateway_pb2_grpc, gateway_pb2
from src.manager.manager import generate_client_id_in_other_peer
from src.utils import logger as l, logger
from src.utils.env import CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME, CLIENT_EXPIRATION_TIME, DOCKER_CLIENT, \
    DOCKER_NETWORK, REMOVE_CONTAINERS, STORAGE, DATABASE_FILE
from src.utils.singleton import Singleton
from src.utils.utils import get_network_name, from_gas_amount, generate_uris_by_peer_id









class SystemCache(metaclass=Singleton):
    _connection = None
    _lock = Lock()

    def __init__(self):
        if not os.path.exists(STORAGE):
            os.makedirs(STORAGE)
        if SystemCache._connection is None:
            SystemCache._connection = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
            SystemCache._connection.row_factory = sqlite3.Row
            self.cursor = SystemCache._connection.cursor()

    # Método para asegurar la conexión persistente
    def _execute(self, query, params=()):
        with SystemCache._lock:
            try:
                self.cursor.execute(query, params)
                SystemCache._connection.commit()
            except sqlite3.Error as e:
                SystemCache._connection.rollback()
                raise e
            return self.cursor

    # Clientes

    def get_clients(self) -> List[dict]:
        """
        Fetches all clients from the database.

        Returns:
            List[dict]: A list of dictionaries containing client details.
        """
        try:
            result = self._execute("SELECT id, gas, last_usage FROM clients")
            clients = [{'id': row[0], 'gas': row[1], 'last_usage': row[2]} for row in result.fetchall()]
            logger.LOGGER(f'Found clients: {clients}')
            return clients
        except sqlite3.Error as e:
            logger.LOGGER(f'Error fetching clients: {e}')
            return []

    def get_clients_id(self) -> List[str]:
        """
        Fetches all client IDs from the database.

        Returns:
            List[str]: A list of client IDs.
        """
        result = self._execute('SELECT id FROM clients')
        return [row['id'] for row in result.fetchall()]

    def __get_client_gas(self, client_id: str) -> Tuple[int, float]:
        result = self._execute('''
            SELECT gas, last_usage FROM clients WHERE id = ?
        ''', (client_id,))
        row = result.fetchone()
        if row:
            return row['gas'], row['last_usage']
        raise Exception(f'Client not found: {client_id}')

    def __update_client(self, client_id: str, gas: int, last_usage: float):
        self._execute('''
            UPDATE clients SET gas = ?, last_usage = ? WHERE id = ?
        ''', (gas, last_usage, client_id))

    def delete_client(self, client_id: str):
        self._execute('''
            DELETE FROM clients WHERE id = ?
        ''', (client_id,))

    def add_gas(self, client_id: str, gas: int = 0):
        _gas, _last_usage = self.__get_client_gas(client_id)
        gas += _gas
        if _last_usage and gas >= CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME:
            _last_usage = None
        self.__update_client(client_id, gas, _last_usage)

    def reduce_gas(self, client_id: str, gas: int):
        _gas, _last_usage = self.__get_client_gas(client_id)
        _gas -= gas
        if _gas == 0 and _last_usage is None:
            _last_usage = time.time()
        self.__update_client(client_id, _gas, _last_usage)

    def client_expired(self, client_id: str) -> bool:
        _gas, _last_usage = self.__get_client_gas(client_id)
        return _last_usage is not None and ((time.time() - _last_usage) >= CLIENT_EXPIRATION_TIME)

    # Peers

    def get_peers(self) -> List[dict]:
        result = self._execute('''
            SELECT id, token, client_id, gas FROM peer
        ''')
        return [dict(row) for row in result.fetchall()]

    def get_peers_id(self) -> List[str]:
        """
        Fetches all peer IDs from the database.

        Returns:
            List[str]: A list of peer IDs.
        """

        # Query to select all peer IDs
        query = "SELECT id FROM peer"

        try:
            result = self._execute(query)
            peer_ids = [row[0] for row in result.fetchall()]
            logger.LOGGER(f'Found peer IDs: {peer_ids}')
            return peer_ids
        except sqlite3.Error as e:
            logger.LOGGER(f'Error fetching peer IDs: {e}')
            return []

    def add_peer(self, peer_id: str) -> bool:
        """
        Adds a peer to the database. Checks if the peer already exists, and if not,
        inserts a new record with the peer_id. The client_id is added separately
        once it is generated.

        Args:
            peer_id (str): The ID of the peer to add.

        Returns:
            bool: True if the peer was successfully added, False otherwise.
        """
        logger.LOGGER(f'Attempting to add peer {peer_id}')

        # Check if the peer already exists in the database
        result = self._execute('''
            SELECT COUNT(*)
            FROM peer
            WHERE id = ?
        ''', (peer_id,))
        exists = result.fetchone()[0]

        # If the peer does not exist, insert it
        if not exists:
            try:
                self._execute('''
                    INSERT INTO peer (id, token, metadata, app_protocol, client_id, gas)
                    VALUES (?, '', NULL, NULL, '', 0)  -- Initialize with empty client_id and 0 gas
                ''', (peer_id,))
                logger.LOGGER(f'Peer {peer_id} added without client_id')
                return True
            except sqlite3.Error as e:
                logger.LOGGER(f'Failed to add peer {peer_id}: {e}')
                return False
        else:
            logger.LOGGER(f'Peer {peer_id} already exists')
            return False

    def add_external_client(self, peer_id: str, client_id: str) -> bool:
        """
        Associates an external client ID with an existing peer.

        Args:
            peer_id (str): The ID of the peer to associate with the client.
            client_id (str): The external client ID to associate with the peer.

        Returns:
            bool: True if the association was successful, False otherwise.
        """
        logger.LOGGER(f'Attempting to add external client {client_id} for peer {peer_id}')

        # Check if the peer exists in the database
        result = self._execute('''
            SELECT COUNT(*)
            FROM peer
            WHERE id = ?
        ''', (peer_id,))
        peer_exists = result.fetchone()[0]

        if not peer_exists:
            logger.LOGGER(f'Peer {peer_id} does not exist in the database')
            return False

        # Update the peer to associate with the new client
        try:
            self._execute('''
                UPDATE peer
                SET client_id = ?
                WHERE id = ?
            ''', (client_id, peer_id))
            logger.LOGGER(f'Associated external client {client_id} with peer {peer_id}')
            return True
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to associate external client {client_id} with peer {peer_id}: {e}')
            return False

    # Método para agregar datos a internal_services
    def add_internal_service(self, id: str, ip: str, token: str, father_id: str):
        self._execute('''
            INSERT INTO internal_services (id, ip, token, father_id)
            VALUES (?, ?, ?, ?)
        ''', (id, ip, token, father_id))

    # Método para agregar datos a external_services
    def add_external_service(self, id: str, ip: str, token: str, token_hash: str):
        self._execute('''
            INSERT INTO external_services (id, ip, token, token_hash)
            VALUES (?, ?, ?, ?)
        ''', (id, ip, token, token_hash))

    # Método para agregar datos a system_cache
    def add_system_cache(self, token: str, mem_limit: int, gas: int):
        self._execute('''
            INSERT INTO peer (id, token, gas)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET token=excluded.token, gas=excluded.gas
        ''', (token, mem_limit, gas))

    # Método para agregar datos a clients
    def add_client(self, client_id: str, gas: int, last_usage: float):
        self._execute('''
            INSERT INTO clients (id, gas, last_usage)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET gas=excluded.gas, last_usage=excluded.last_usage
        ''', (client_id, gas, last_usage))

    # Método para actualizar el gas de un cliente en clients
    def update_client_gas(self, client_id: str, gas: int):
        self._execute('''
            UPDATE clients
            SET gas = ?
            WHERE id = ?
        ''', (gas, client_id))

    # Método para obtener el token por uri
    def get_token_by_uri(self, uri: str) -> str:
        result = self._execute('''
            SELECT token
            FROM internal_services
            WHERE ip = ?
        ''', (uri,))
        row = result.fetchone()
        if row:
            return row['token']
        raise Exception(f'Token not found for URI: {uri}')

    # Método para obtener la cantidad de gas por id
    def get_gas_amount_by_id(self, id: str) -> int:
        result = self._execute('''
            SELECT gas
            FROM clients
            WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        if row:
            return row['gas']
        raise Exception(f'Gas amount not found for ID: {id}')

    # Método para purgar un servicio interno
    def purge_internal(self, agent_id=None, container_id=None, container_ip=None, token=None) -> int:
        if token is None and (agent_id is None or container_id is None or container_ip is None):
            raise Exception(
                'purge_internal: token is None and (agent_id is None or container_id is None or container_ip is None)'
            )

        refund = self.get_gas_amount_by_id(container_ip)

        self._execute('''
            DELETE FROM internal_services
            WHERE id = ? AND father_id = ?
        ''', (container_id, agent_id))

        self._execute('''
            DELETE FROM peer
            WHERE token = ?
        ''', (token,))

        return refund

    # Método para purgar un servicio externo
    def purge_external(self, agent_id, peer_id, his_token) -> int:
        refund = 0

        self._execute('''
            DELETE FROM external_services
            WHERE id = ? AND token = ?
        ''', (peer_id, his_token))

        return refund

    # Método para eliminar dependencias de contenedores en la tabla internal_services
    def remove_container_dependency(self, agent_id: str, dependency: str):
        self._execute('''
            DELETE FROM internal_services
            WHERE id = ? AND father_id = ?
        ''', (dependency, agent_id))

    # Método para eliminar dependencias de contenedores en la tabla external_services
    def remove_external_dependency(self, agent_id: str, dependency: str):
        self._execute('''
            DELETE FROM external_services
            WHERE id = ? AND token = ?
        ''', (dependency, agent_id))

    def set_on_cache(self,
                     agent_id: str,
                     container_ip___peer_id: str,
                     container_id___his_token_encrypt: str,
                     container_id____his_token: str
                     ):



        # En caso de ser un nodo externo:
        if not agent_id in self.container_cache:                # <-- NO SE HACE NADA?
            self.container_cache_lock.acquire()
            self.container_cache.update({agent_id: []})
            self.container_cache_lock.release()
            # Si peer_ip es un servicio del nodo ya
            # debería estar en el registro.

        # Añade el nuevo servicio como dependencia.
        self.container_cache[agent_id].append(container_ip___peer_id + '##' + container_id___his_token_encrypt)  # <-- SE AGREGA UN NUEVO SERVICIO.
        self.cache_service_perspective[container_ip___peer_id] = container_id____his_token
        l.LOGGER(
            'Set on cache ' + container_ip___peer_id + '##' + container_id___his_token_encrypt + ' as dependency of ' + agent_id)

    def set_external_on_cache(self, client_id: str, encrypted_external_token: str, external_token: str, peer_id: str):
        self._execute('''
            INSERT INTO external_services (token, token_hash, peer_id, client_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (external_token, encrypted_external_token, peer_id, client_id))

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
