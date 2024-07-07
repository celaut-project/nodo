import os
import sqlite3
import time
from threading import Lock
from typing import List, Tuple, TypedDict, Optional

import docker as docker_lib
import grpc
from grpcbigbuffer import client as grpcbf

from protos import gateway_pb2_grpc, gateway_pb2
from src.utils import logger as l, logger
from src.utils.env import CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME, CLIENT_EXPIRATION_TIME, DOCKER_CLIENT, \
    DOCKER_NETWORK, REMOVE_CONTAINERS, STORAGE, DATABASE_FILE, DEFAULT_INTIAL_GAS_AMOUNT
from src.utils.singleton import Singleton
from src.utils.utils import get_network_name, from_gas_amount, generate_uris_by_peer_id


class SQLConnection(metaclass=Singleton):
    _connection = None
    _lock = Lock()

    def __init__(self):
        if not os.path.exists(STORAGE):
            os.makedirs(STORAGE)
        if SQLConnection._connection is None:
            SQLConnection._connection = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
            SQLConnection._connection.row_factory = sqlite3.Row
            self.cursor = SQLConnection._connection.cursor()

    # Método para asegurar la conexión persistente
    def _execute(self, query, params=()):
        with SQLConnection._lock:
            try:
                self.cursor.execute(query, params)
                SQLConnection._connection.commit()
            except sqlite3.Error as e:
                SQLConnection._connection.rollback()
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

    def client_exists(self, client_id: str) -> bool:
        # Check if the peer exists in the database
        result = self._execute('''
            SELECT COUNT(*)
            FROM clients
            WHERE id = ?
        ''', (client_id,))
        return result.fetchone()[0]

    def get_client_gas(self, client_id: str) -> Tuple[int, float]:
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
        _gas, _last_usage = self.get_client_gas(client_id)
        gas += _gas
        if _last_usage and gas >= CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME:
            _last_usage = None
        self.__update_client(client_id, gas, _last_usage)

    def reduce_gas(self, client_id: str, gas: int):
        _gas, _last_usage = self.get_client_gas(client_id)
        _gas -= gas
        if _gas == 0 and _last_usage is None:
            _last_usage = time.time()
        self.__update_client(client_id, _gas, _last_usage)

    def update_sys_req(self, token: str, mem_limit: Optional[int]) -> bool:
        try:
            self._execute('''
                UPDATE internal_services SET mem_limit = ? WHERE token = ?
            ''', (mem_limit, token))
            return True
        except:
            return False

    def get_sys_req(self, token: str) -> dict:
        result = self._execute('''
            SELECT mem_limit FROM internal_services WHERE token = ?
        ''', (token,))
        row = result.fetchone()
        if row:
            return row
        raise Exception(f'Internal service {token}')

    def get_internal_service_gas(self, token: str) -> int:
        result = self._execute('''
            SELECT gas FROM internal_services WHERE token = ?
        ''', (token,))
        row = result.fetchone()
        if row:
            return row['gas']
        raise Exception(f'Internal service {token}')

    def client_expired(self, client_id: str) -> bool:
        _gas, _last_usage = self.get_client_gas(client_id)
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

        # If the peer does not exist, insert it
        if not self.peer_exists(peer_id=peer_id):
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

    def peer_exists(self, peer_id: str) -> bool:
        # Check if the peer exists in the database
        result = self._execute('''
            SELECT COUNT(*)
            FROM peer
            WHERE id = ?
        ''', (peer_id,))
        return result.fetchone()[0]

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

        peer_exists = self.peer_exists(peer_id=peer_id)

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

    # Método para agregar datos a clients
    def add_client(self, client_id: str, gas: int, last_usage: Optional[float]):
        self._execute('''
            INSERT INTO clients (id, gas, last_usage)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET gas=excluded.gas, last_usage=excluded.last_usage
        ''', (client_id, gas, last_usage))

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
    def get_gas_amount_by_client_id(self, id: str) -> int:
        result = self._execute('''
            SELECT gas
            FROM clients
            WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        if row:
            return row['gas']
        raise Exception(f'Gas amount not found for ID: {id}')

    def get_gas_amount_by_father_id(self, id: str) -> int:
        if self.client_exists(client_id=id):
            return self.get_gas_amount_by_client_id(id=id)
        elif self.container_exists(token=id):
            return self.get_internal_service_gas(token=id)
        else:
            return int(DEFAULT_INTIAL_GAS_AMOUNT)

    def add_internal_service(self,
                             father_id: str,
                             container_ip: str,
                             container_id: str,
                             token: str,
                             gas: int
                             ):
        self._execute('''
            INSERT INTO internal_services (id, ip, token, father_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (container_id, container_ip, token, father_id, gas))
        l.LOGGER(f'Set on cache {token} as dependency of {father_id}')

    def update_gas_to_container(self, token: str, gas: int):
        self._execute('''
            UPDATE internal_services SET gas = ? WHERE token = ?
        ''', (gas, gas, token))

    def container_exists(self, token: str) -> bool:
        # Check if the peer exists in the database
        result = self._execute('''
            SELECT COUNT(*)
            FROM internal_services
            WHERE token = ?
        ''', (token,))
        return result.fetchone()[0]

    def add_external_service(self, client_id: str, encrypted_external_token: str, external_token: str, peer_id: str):
        self._execute('''
            INSERT INTO external_services (token, token_hash, peer_id, client_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (external_token, encrypted_external_token, peer_id, client_id))

    def purge_internal(self, agent_id=None, container_id=None, container_ip=None, token=None) -> int:
        if token is None and (agent_id is None or container_id is None or container_ip is None):
            raise Exception(
                'purge_internal: token is None and (agent_id is None or container_id is None or container_ip is None)'
            )

        if REMOVE_CONTAINERS:
            try:
                DOCKER_CLIENT().containers.get(container_id).remove(force=True)
            except (docker_lib.errors.NotFound, docker_lib.errors.APIError) as e:
                l.LOGGER(str(e) + 'ERROR WITH DOCKER WHEN TRYING TO REMOVE THE CONTAINER ' + container_id)
                return 0

        refund = self.get_internal_service_gas(token=token)

        self._execute('''
            DELETE FROM internal_services
            WHERE id = ? AND father_id = ?
        ''', (container_id, agent_id))

        return refund

    def purge_external(self, agent_id, peer_id, his_token) -> int:
        refund = 0

        hashed_token = self._execute('''
            SELECT token_hash
            FROM external_services
            WHERE token = ?
        ''', (his_token,)).fetchone()["token_hash"]

        self._execute('''
            DELETE FROM external_services
            WHERE token = ?
        ''', (his_token,))

        try:
            refund = from_gas_amount(next(grpcbf.client_grpc(
                method=gateway_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(
                        next(generate_uris_by_peer_id(peer_id=peer_id))
                    )
                ).StopService,
                input=gateway_pb2.TokenMessage(
                    token=hashed_token
                ),
                indices_parser=gateway_pb2.Refund,
                partitions_message_mode_parser=True
            )).amount)
        except grpc.RpcError as e:
            l.LOGGER('Error during remove a container on ' + peer_id + ' ' + str(e))

        return refund

def is_peer_available(peer_id: str, min_slots_open: int = 1) -> bool:
    SQLConnection().peer_exists(peer_id=peer_id)
    try:
        return any(list(generate_uris_by_peer_id(peer_id))) if min_slots_open == 1 else \
            len(list(generate_uris_by_peer_id(peer_id))) >= min_slots_open
    except Exception:
        return False