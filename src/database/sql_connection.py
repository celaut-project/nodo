import os
import sqlite3
import time
from hashlib import sha3_256
from threading import Lock
from typing import List, Tuple, Optional

import docker as docker_lib
import grpc
from grpcbigbuffer import client as grpcbf

from protos import gateway_pb2_grpc, gateway_pb2, celaut_pb2
from src.utils import logger as l, logger
from src.utils.env import (
    CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME,
    CLIENT_EXPIRATION_TIME,
    DOCKER_CLIENT,
    REMOVE_CONTAINERS,
    STORAGE,
    DATABASE_FILE,
    DEFAULT_INTIAL_GAS_AMOUNT, SHA3_256_ID
)
from src.utils.singleton import Singleton
from src.utils.utils import from_gas_amount, generate_uris_by_peer_id

# Define a maximum mantissa and exponent
MAX_MANTISSA = 10**9  # Adjust this limit as needed
MAX_EXPONENT = 128  # Adjust this limit as needed


def get_internal_service_id_by_token(token: str) -> str:
    """Extracts the internal service ID from a token."""
    return token.split("##")[2]


def _combine_gas(mantissa: int, exponent: int) -> int:
    """
    Combines mantissa and exponent into a single gas amount.

    Args:
        mantissa (int): The mantissa of the gas amount.
        exponent (int): The exponent of the gas amount.

    Returns:
        int: The combined gas amount.
    """
    return mantissa * (10 ** exponent)


def _validate_gas(mantissa: int, exponent: int):
    """
    Validates the gas amount to ensure it is within acceptable limits.

    Args:
        mantissa (int): The mantissa of the gas amount.
        exponent (int): The exponent of the gas amount.

    Raises:
        ValueError: If the gas amount exceeds the maximum limit.
    """
    if mantissa < 0 or mantissa > MAX_MANTISSA:
        raise ValueError(f"Mantissa {mantissa} is out of acceptable range (0 to {MAX_MANTISSA})")
    if exponent < 0 or exponent > MAX_EXPONENT:
        raise ValueError(f"Exponent {exponent} is out of acceptable range (0 to {MAX_EXPONENT})")


def _split_gas(gas: int) -> Tuple[int, int]:
    """
    Splits a gas amount into mantissa and exponent.

    Args:
        gas (int): The gas amount.

    Returns:
        Tuple[int, int]: The mantissa and exponent.
    """
    exponent = 0
    while gas >= MAX_MANTISSA and exponent < MAX_EXPONENT:
        gas //= 10
        exponent += 1

    # Ensure the mantissa is within range
    if gas > MAX_MANTISSA:
        raise ValueError(f"Splitted mantissa {gas} is out of acceptable range (0 to {MAX_MANTISSA})")

    return gas, exponent


class SQLConnection(metaclass=Singleton):
    _connection = None
    _lock = Lock()

    def __init__(self):
        """Initializes the SQLConnection, ensuring storage directory and establishing a database connection."""
        if not os.path.exists(STORAGE):
            os.makedirs(STORAGE)
        if SQLConnection._connection is None:
            SQLConnection._connection = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
            SQLConnection._connection.row_factory = sqlite3.Row
            self.cursor = SQLConnection._connection.cursor()

    def _execute(self, query: str, params=()) -> sqlite3.Cursor:
        """
        Executes a query with the given parameters, ensuring thread safety.

        Args:
            query (str): The SQL query to execute.
            params (tuple): The parameters to bind to the query.

        Returns:
            sqlite3.Cursor: The cursor for the executed query.
        """
        with SQLConnection._lock:
            try:
                self.cursor.execute(query, params)
                SQLConnection._connection.commit()
            except sqlite3.Error as e:
                SQLConnection._connection.rollback()
                raise e
            return self.cursor

    # Client Methods

    def add_client(self, client_id: str, gas: int, last_usage: Optional[float]):
        """
        Adds a client to the database, updating if a conflict occurs.

        Args:
            client_id (str): The ID of the client.
            gas (int): The gas amount.
            last_usage (Optional[float]): The last usage time.
        """
        gas_mantissa, gas_exponent = _split_gas(gas)
        _validate_gas(gas_mantissa, gas_exponent)
        self._execute('''
            INSERT INTO clients (id, gas_mantissa, gas_exponent, last_usage)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET gas_mantissa=excluded.gas_mantissa, gas_exponent=excluded.gas_exponent, last_usage=excluded.last_usage
        ''', (client_id, gas_mantissa, gas_exponent, last_usage))

    def get_clients(self) -> List[dict]:
        """
        Fetches all clients from the database.

        Returns:
            List[dict]: A list of dictionaries containing client details.
        """
        try:
            result = self._execute("SELECT id, gas_mantissa, gas_exponent, last_usage FROM clients")
            clients = [{'id': row[0], 'gas_mantissa': row[1], 'gas_exponent': row[2], 'last_usage': row[3]} for row in result.fetchall()]
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
        """
        Checks if a client exists in the database.

        Args:
            client_id (str): The ID of the client to check.

        Returns:
            bool: True if the client exists, False otherwise.
        """
        result = self._execute('''
            SELECT COUNT(*)
            FROM clients
            WHERE id = ?
        ''', (client_id,))
        return result.fetchone()[0] > 0

    def get_dev_clients(self) -> List[str]:
        """
        Fetches all client IDs that start with 'dev-' from the database.

        Returns:
            List[str]: A list of client IDs that start with 'dev-'.
        """
        result = self._execute('SELECT id FROM clients WHERE id LIKE ?', ('dev-%',))
        return [row['id'] for row in result.fetchall()]

    def get_client_gas(self, client_id: str) -> Tuple[int, float]:
        """
        Retrieves the gas and last usage time for a client.

        Args:
            client_id (str): The ID of the client.

        Returns:
            Tuple[int, int, float]: The mantissa, exponent of gas amount, and last usage time.
        """
        result = self._execute('''
            SELECT gas_mantissa, gas_exponent, last_usage FROM clients WHERE id = ?
        ''', (client_id,))
        row = result.fetchone()
        if row:
            return _combine_gas(mantissa=row['gas_mantissa'], exponent=row['gas_exponent']), row['last_usage']
        raise Exception(f'Client not found: {client_id}')

    def delete_client(self, client_id: str):
        """Deletes a client from the database."""
        self._execute('''
            DELETE FROM clients WHERE id = ?
        ''', (client_id,))

    def add_gas(self, client_id: str, gas: int = 0):
        """
        Adds gas to a client's balance.

        Args:
            client_id (str): The ID of the client.
            gas (int): The amount of gas to add.
        """
        _gas, _last_usage = self.get_client_gas(client_id)
        total_gas = _gas + gas
        new_mantissa, new_exponent = _split_gas(total_gas)
        _validate_gas(new_mantissa, new_exponent)
        if _last_usage and total_gas >= CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME:
            _last_usage = None
        self.__update_client(client_id, new_mantissa, new_exponent, _last_usage)

    def reduce_gas(self, client_id: str, gas: int):
        """
        Reduces gas from a client's balance.

        Args:
            client_id (str): The ID of the client.
            gas (int): The amount of gas to reduce.
        """
        _gas, _last_usage = self.get_client_gas(client_id)
        total_gas = _gas - gas
        new_mantissa, new_exponent = _split_gas(total_gas)
        _validate_gas(new_mantissa, new_exponent)
        if total_gas == 0 and _last_usage is None:
            _last_usage = time.time()
        self.__update_client(client_id, new_mantissa, new_exponent, _last_usage)

    def client_expired(self, client_id: str) -> bool:
        """
        Checks if a client has expired.

        Args:
            client_id (str): The ID of the client.

        Returns:
            bool: True if the client has expired, False otherwise.
        """
        _gas, _last_usage = self.get_client_gas(client_id)
        return _last_usage is not None and ((time.time() - _last_usage) >= CLIENT_EXPIRATION_TIME)

    def __update_client(self, client_id: str, mantissa: int, exponent: int, last_usage: float):
        """Updates the gas and last usage time for a client."""
        _validate_gas(mantissa, exponent)
        self._execute('''
            UPDATE clients SET gas_mantissa = ?, gas_exponent = ?, last_usage = ? WHERE id = ?
        ''', (mantissa, exponent, last_usage, client_id))

    def get_gas_amount_by_client_id(self, id: str) -> int:
        """
        Retrieves the gas amount for a client ID.

        Args:
            id (str): The client ID.

        Returns:
            int: The gas amount.
        """
        result = self._execute('''
            SELECT gas_mantissa, gas_exponent FROM clients WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        if row:
            return row['gas_mantissa'] * (10 ** row['gas_exponent'])
        raise Exception(f'Gas amount not found for ID: {id}')

    # Internal Service Methods

    def add_internal_service(self, father_id: str, container_ip: str, container_id: str, token: str, gas: int):
        """
        Adds an internal service to the database.

        Args:
            father_id (str): The father ID.
            container_ip (str): The IP address of the container.
            container_id (str): The container ID.
            token (str): The token.
            gas (int): The gas amount.
        """
        gas_mantissa, gas_exponent = _split_gas(gas)
        _validate_gas(gas_mantissa, gas_exponent)
        self._execute('''
            INSERT INTO internal_services (id, ip, father_id, gas_mantissa, gas_exponent, mem_limit)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (container_id, container_ip, father_id, gas_mantissa, gas_exponent, 0))
        l.LOGGER(f'Set on cache {token} as dependency of {father_id}')

    def update_sys_req(self, token: str, mem_limit: Optional[int]) -> bool:
        """
        Updates system requirements for an internal service.

        Args:
            token (str): The token of the internal service.
            mem_limit (Optional[int]): The new memory limit.

        Returns:
            bool: True if update was successful, False otherwise.
        """
        try:
            self._execute('''
                UPDATE internal_services SET mem_limit = ? WHERE id = ?
            ''', (mem_limit, get_internal_service_id_by_token(token=token)))
            return True
        except:
            return False

    def get_sys_req(self, token: str) -> dict:
        """
        Retrieves system requirements for an internal service.

        Args:
            token (str): The token of the internal service.

        Returns:
            dict: A dictionary containing the system requirements.
        """
        result = self._execute('''
            SELECT mem_limit FROM internal_services WHERE id = ?
        ''', (get_internal_service_id_by_token(token=token),))
        row = result.fetchone()
        if row:
            return row
        raise Exception(f'Internal service {token}')

    def get_internal_service_gas(self, token: str) -> int:
        """
        Retrieves the gas amount for an internal service.

        Args:
            token (str): The token of the internal service.

        Returns:
            int: The gas amount.
        """
        result = self._execute('''
            SELECT gas_mantissa, gas_exponent FROM internal_services WHERE id = ?
        ''', (get_internal_service_id_by_token(token=token),))
        row = result.fetchone()
        if row:
            return row['gas_mantissa'] * (10 ** row['gas_exponent'])
        raise Exception(f'Internal service {token}')

    def get_all_internal_service_tokens(self) -> List[str]:
        """
        Fetches all tokens of internal services.

        Returns:
            List[str]: A list of tokens.
        """
        result = self._execute('''
            SELECT father_id, ip, id FROM internal_services
        ''')
        return [f"{row['father_id']}##{row['ip']}##{row['id']}" for row in result.fetchall()]

    def update_gas_to_container(self, token: str, gas: int):
        """
        Updates the gas amount for a container.

        Args:
            token (str): The token of the container.
            gas (int): The new gas amount.
        """
        gas_mantissa, gas_exponent = _split_gas(gas)
        _validate_gas(gas_mantissa, gas_exponent)
        self._execute('''
            UPDATE internal_services SET gas_mantissa = ?, gas_exponent = ? WHERE id = ?
        ''', (gas_mantissa, gas_exponent, get_internal_service_id_by_token(token=token)))

    def container_exists(self, token: str) -> bool:
        """
        Checks if a container exists in the database.

        Args:
            token (str): The token of the container.

        Returns:
            bool: True if the container exists, False otherwise.
        """
        result = self._execute('''
            SELECT COUNT(*)
            FROM internal_services
            WHERE id = ?
        ''', (get_internal_service_id_by_token(token=token),))
        return result.fetchone()[0] > 0

    def purge_internal(self, agent_id=None, container_id=None, container_ip=None, token=None) -> int:
        """
        Purges an internal service and optionally removes its Docker container.

        Args:
            agent_id (str, optional): The agent ID.
            container_id (str, optional): The container ID.
            container_ip (str, optional): The container IP.
            token (str, optional): The token of the internal service.

        Returns:
            int: The gas amount refunded.
        """
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

        gas = self.get_internal_service_gas(token=token)

        self._execute('''
            DELETE FROM internal_services WHERE id = ? AND father_id = ?
        ''', (container_id, agent_id))

        return gas

    # Peer Methods

    def get_peers(self) -> List[dict]:
        """
        Fetches all peers from the database.

        Returns:
            List[dict]: A list of dictionaries containing peer details.
        """
        result = self._execute('''
            SELECT id, token, client_id, gas_mantissa, gas_exponent FROM peer
        ''')

        peers = []
        for row in result.fetchall():
            peer = dict(row)
            gas_mantissa = peer.pop('gas_mantissa')
            gas_exponent = peer.pop('gas_exponent')
            peer['gas'] = _combine_gas(gas_mantissa, gas_exponent)
            peers.append(peer)

        return peers

    def get_peers_id(self) -> List[str]:
        """
        Fetches all peer IDs from the database.

        Returns:
            List[str]: A list of peer IDs.
        """
        try:
            result = self._execute("SELECT id FROM peer")
            peer_ids = [row[0] for row in result.fetchall()]
            logger.LOGGER(f'Found peer IDs: {peer_ids}')
            return peer_ids
        except sqlite3.Error as e:
            logger.LOGGER(f'Error fetching peer IDs: {e}')
            return []

    def add_gas_to_peer(self, peer_id: str, gas: int):
        try:
            result = self._execute('SELECT gas_mantissa, gas_exponent FROM peer WHERE id = ?', (peer_id,))
            row = result.fetchone()
            if row:
                current_gas = _combine_gas(row['gas_mantissa'], row['gas_exponent'])
                total_gas = current_gas + gas
                new_mantissa, new_exponent = _split_gas(total_gas)
                _validate_gas(new_mantissa, new_exponent)
                self._execute('''
                    UPDATE peer SET gas_mantissa = ?, gas_exponent = ? WHERE id = ?
                ''', (new_mantissa, new_exponent, peer_id))
                return True
            else:
                raise Exception(f'Peer not found: {peer_id}')
        except Exception as e:
            logger.LOGGER(f'Error adding gas to peer {peer_id}: {e}')
            return False


    def add_peer(self, peer_id: str, token: Optional[str], metadata: Optional[bytes], app_protocol: bytes) -> bool:
        """
        Adds a peer to the database.

        Args:
            peer_id (str): The ID of the peer to add.

        Returns:
            bool: True if the peer was successfully added, False otherwise.
        """
        logger.LOGGER(f'Attempting to add peer {peer_id}')

        if not self.peer_exists(peer_id=peer_id):
            try:
                self._execute('''
                    INSERT INTO peer (id, token, metadata, app_protocol, client_id, gas_mantissa, gas_exponent)
                    VALUES (?, ?, ?, ?, '', 0, 0)  -- Initialize with empty client_id and 0 gas
                ''', (peer_id, token, metadata, app_protocol))
                logger.LOGGER(f'Peer {peer_id} added without client_id')
                return True
            except sqlite3.Error as e:
                logger.LOGGER(f'Failed to add peer {peer_id}: {e}')
                return False
        else:
            logger.LOGGER(f'Peer {peer_id} already exists')
            return False

    def add_slot(self, slot: celaut_pb2.Instance.Uri_Slot, peer_id: str):
        """
        Adds a slot to the database.

        Args:
            slot (celaut_pb2.Instance.Uri_Slot): The slot to add.
            peer_id (str): The ID of the peer.
        """
        internal_port: int = slot.internal_port
        transport_protocol: bytes = bytes("tcp", "utf-8")
        cursor = self._execute("INSERT INTO slot (internal_port, transport_protocol, peer_id) VALUES (?, ?, ?)",
                               (internal_port, transport_protocol, peer_id))
        slot_id = cursor.lastrowid
        if slot_id:
            slot_id = str(slot_id)
            for uri in slot.uri:
                self.add_uri(uri, slot_id=slot_id)

    def add_contract(self, contract: celaut_pb2.Service.Api.ContractLedger, peer_id: str):
        """
        Adds a contract to the database.

        Args:
            contract (celaut_pb2.Service.Api.ContractLedger): The contract to add.
            peer_id (str): The ID of the peer.
        """
        contract_content: bytes = contract.contract
        address: str = contract.contract_addr
        ledger: str = contract.ledger

        contract_hash: str = sha3_256(contract_content).hexdigest()
        contract_hash_type: str = SHA3_256_ID.hex()

        self._execute("INSERT OR IGNORE INTO contract (hash, hash_type, contract) VALUES (?,?,?)",
                      (contract_hash, contract_hash_type, contract_content))

        self._execute("INSERT OR IGNORE INTO ledger (id) VALUES (?)",
                      (ledger,))

        self._execute("INSERT INTO contract_instance (address, ledger_id, contract_hash, peer_id) "
                      "VALUES (?,?,?,?)", (address, ledger, contract_hash, peer_id))

    def peer_exists(self, peer_id: str) -> bool:
        """
        Checks if a peer exists in the database.

        Args:
            peer_id (str): The ID of the peer to check.

        Returns:
            bool: True if the peer exists, False otherwise.
        """
        result = self._execute('''
            SELECT COUNT(*)
            FROM peer
            WHERE id = ?
        ''', (peer_id,))
        return result.fetchone()[0] > 0

    def add_external_client(self, peer_id: str, client_id: str) -> bool:
        """
        Associates an external client ID with an existing peer.

        Args:
            peer_id (str): The ID of the peer.
            client_id (str): The external client ID to associate.

        Returns:
            bool: True if the association was successful, False otherwise.
        """
        logger.LOGGER(f'Attempting to add external client {client_id} for peer {peer_id}')

        if not self.peer_exists(peer_id=peer_id):
            logger.LOGGER(f'Peer {peer_id} does not exist in the database')
            return False

        try:
            self._execute('''
                UPDATE peer SET client_id = ? WHERE id = ?
            ''', (client_id, peer_id))
            logger.LOGGER(f'Associated external client {client_id} with peer {peer_id}')
            return True
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to associate external client {client_id} with peer {peer_id}: {e}')
            return False

    def peer_has_client(self, peer_id: str) -> bool:
        """
        Checks if a peer has an associated client.

        Args:
            peer_id (str): The ID of the peer.

        Returns:
            bool: True if the peer has both a client, False otherwise.
        """
        try:
            result = self._execute('''
                SELECT client_id FROM peer WHERE id = ?
            ''', (peer_id,))
            row = result.fetchone()
            if row and row['client_id']:
                return True
            return False
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to check client for peer {peer_id}: {e}')
            return False

    def get_peer_client(self, peer_id: str) -> Optional[str]:
        """
        Retrieves the client ID associated with a peer.

        Args:
            peer_id (str): The ID of the peer.

        Returns:
            str: The client ID if it exists, or None if not found.
        """
        try:
            result = self._execute('''
                SELECT client_id FROM peer WHERE id = ?
            ''', (peer_id,))
            row = result.fetchone()
            if row:
                return row['client_id']
            return None
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to retrieve client for peer {peer_id}: {e}')
            return None

    def delete_external_client(self, peer_id: str):
        """
        Deletes the external client from a peer.

        Args:
            peer_id (str): The ID of the peer.
        """
        try:
            self._execute('''
                UPDATE peer SET client_id = NULL WHERE id = ?
            ''', (peer_id,))
            logger.LOGGER(f'Successfully deleted external client associated with peer {peer_id}')
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to delete external client associated with peer {peer_id}: {e}')
            pass

    def add_uri(self, uri: celaut_pb2.Instance.Uri, slot_id: str):
        """
        Adds a URI to the database.

        Args:
            uri (celaut_pb2.Instance.Uri): The URI to add.
            slot_id (str): The ID of the slot.
        """
        ip: str = uri.ip
        port: int = uri.port
        self._execute("INSERT INTO uri (ip, port, slot_id) VALUES (?, ?, ?)",
                      (ip, port, slot_id))

    def add_external_service(self, client_id: str, encrypted_external_token: str, external_token: str, peer_id: str):
        """
        Adds an external service to the database.

        Args:
            client_id (str): The client ID.
            encrypted_external_token (str): The encrypted external token.
            external_token (str): The external token.
            peer_id (str): The peer ID.
        """
        self._execute('''
            INSERT INTO external_services (token, token_hash, peer_id, client_id)
            VALUES (?, ?, ?, ?)
        ''', (external_token, encrypted_external_token, peer_id, client_id))

    def get_token_by_hashed_token(self, hashed_token: str) -> Optional[str]:
        """
        Retrieves the token associated with a given hashed token for an external service.

        Args:
            hashed_token (str): The hashed token of the external service.

        Returns:
            Optional[str]: The token if it exists, or None if not found.
        """
        try:
            result = self._execute('''
                SELECT token FROM external_services WHERE token_hash = ?
            ''', (hashed_token,))
            row = result.fetchone()
            if row:
                return row['token']
            return None
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to retrieve token for hashed token {hashed_token}: {e}')
            return None

    def purge_external(self, agent_id: str, peer_id: str, his_token: str) -> int:
        """
        Purges an external service and refunds gas.

        Args:
            agent_id (str): The agent ID.
            peer_id (str): The peer ID.
            his_token (str): The token of the external service.

        Returns:
            int: The gas amount refunded.
        """
        refund = 0

        hashed_token = self._execute('''
            SELECT token_hash FROM external_services WHERE token = ?
        ''', (his_token,)).fetchone()["token_hash"]

        self._execute('''
            DELETE FROM external_services WHERE token = ?
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

    # Common Methods

    def get_token_by_uri(self, uri: str) -> str:
        """
        Retrieves the token for a given URI.

        Args:
            uri (str): The URI to look up.

        Returns:
            str: The associated token.
        """
        result = self._execute('''
            SELECT token FROM internal_services WHERE ip = ?
        ''', (uri,))
        row = result.fetchone()
        if row:
            return row['token']
        raise Exception(f'Token not found for URI: {uri}')

    def get_gas_amount_by_father_id(self, id: str) -> int:
        """
        Retrieves the gas amount for a father ID, checking both clients and internal services.

        Args:
            id (str): The father ID.

        Returns:
            int: The gas amount.
        """
        if self.client_exists(client_id=id):
            return self.get_gas_amount_by_client_id(id=id)
        elif self.container_exists(token=id):
            return self.get_internal_service_gas(token=id)
        else:
            return int(DEFAULT_INTIAL_GAS_AMOUNT)

def is_peer_available(peer_id: str, min_slots_open: int = 1) -> bool:
    """
    Checks if a peer is available based on the number of open slots.

    Args:
        peer_id (str): The ID of the peer.
        min_slots_open (int): Minimum number of open slots required.

    Returns:
        bool: True if the peer is available, False otherwise.
    """
    SQLConnection().peer_exists(peer_id=peer_id)
    try:
        return any(list(generate_uris_by_peer_id(peer_id))) if min_slots_open == 1 else \
            len(list(generate_uris_by_peer_id(peer_id))) >= min_slots_open
    except Exception:
        return False
