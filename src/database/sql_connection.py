import datetime
import os
import math
import uuid
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from hashlib import sha3_256
from threading import Lock
from typing import Any, Callable, Dict, Generator, Iterable, List, Tuple, Optional
from google.protobuf.json_format import MessageToJson

import grpc
from bee_rpc import client as bee

from protos import celaut_pb2_grpc, celaut_pb2, celaut_pb2
from src.utils import logger as log, logger
from src.utils.contract_xattrs import contract_shape_bytes, get_address, get_contract_type, get_script, get_token_id
from src.utils.config import ConfigManager
from src.utils.singleton import Singleton
from src.utils.utils import from_amount, generate_uris_by_peer_id
from src.utils.monetary import format_mu

env_manager = ConfigManager()

CLIENT_MIN_BALANCE_MU_TO_RESET_EXPIRATION = int(
    env_manager.get("client.CLIENT_MIN_BALANCE_MU_TO_RESET_EXPIRATION", 0) or 0
)
TOTAL_REPUTATION_TOKEN_AMOUNT = int(env_manager.get("ledgers.ergo.reputation.TOTAL_REPUTATION_TOKEN_AMOUNT"))
CLIENT_EXPIRATION_TIME = env_manager.get("CLIENT_EXPIRATION_TIME")
STORAGE = env_manager.get("STORAGE")
DATABASE_FILE = env_manager.get("DATABASE_FILE")


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
                # Create a new cursor for each execution
                cursor = SQLConnection._connection.cursor()
                cursor.execute(query, params)
                SQLConnection._connection.commit()
                return cursor
            except sqlite3.Error as e:
                SQLConnection._connection.rollback()
                raise e

    def _execute2(self, query_or_queries, params=()) -> sqlite3.Cursor:
        """
        Executes a query or a list of queries with the given parameters, ensuring thread safety.

        Args:
            query_or_queries: Either a single SQL query string or a list of (query, params) tuples
            params (tuple): The parameters to bind to the query (only used for single query)

        Returns:
            sqlite3.Cursor: The cursor for the executed query(ies).
        """
        with SQLConnection._lock:
            try:
                cursor = SQLConnection._connection.cursor()
                
                # Check if it's a single query or multiple queries
                if isinstance(query_or_queries, str):
                    # Single query execution
                    cursor.execute(query_or_queries, params)
                elif isinstance(query_or_queries, list):
                    # Multiple queries execution (batch)
                    cursor.execute('BEGIN TRANSACTION')
                    try:
                        for query_item in query_or_queries:
                            if isinstance(query_item, tuple):
                                query, query_params = query_item
                                cursor.execute(query, query_params)
                            else:
                                # Assume it's just a query string without parameters
                                cursor.execute(query_item, ())
                        cursor.execute('COMMIT')
                    except sqlite3.Error as batch_error:
                        cursor.execute('ROLLBACK')
                        raise batch_error
                else:
                    raise ValueError("query_or_queries must be either a string or a list of (query, params) tuples")
                
                SQLConnection._connection.commit()
                return cursor
                
            except sqlite3.Error as e:
                SQLConnection._connection.rollback()
                raise e

    # Client Methods

    def add_client(self, client_id: str, balance_mu: int, last_usage: Optional[float],
                   unmetered: bool = False):
        """
        Adds a client to the database, updating if a conflict occurs.

        Args:
            client_id (str): The ID of the client.
            balance_mu (int): Its starting balance, in MU.
            last_usage (Optional[float]): The last usage time.
            unmetered (bool): Never charge this client. Set for the node's own dev
                clients, which used to express the same thing by holding a balance of
                1e256 -- a flag written as a number.
        """
        self._execute('''
            INSERT INTO clients (id, balance_mu, last_usage, unmetered)
            VALUES (?, ?, ?, ?)
        ''', (client_id, str(balance_mu), last_usage, int(bool(unmetered))))

    def client_is_unmetered(self, client_id: str) -> bool:
        """True for a client the node never charges (its own dev clients)."""
        row = self._execute(
            'SELECT unmetered FROM clients WHERE id = ?', (client_id,)
        ).fetchone()
        return bool(row['unmetered']) if row else False

    def get_clients(self) -> List[dict]:
        """
        Fetches all clients from the database.

        Returns:
            List[dict]: A list of dictionaries containing client details.
        """
        try:
            result = self._execute("SELECT id, balance_mu, last_usage FROM clients")
            clients = [{'id': row[0], 'balance_mu': row[1], 'last_usage': row[2]} for row in result.fetchall()]
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

    def get_client_balance(self, client_id: str) -> Optional[Tuple[int, float, str]]:
        """
        Retrieves the balance_mu and last usage time for a client.

        Args:
            client_id (str): The ID of the client.

        Returns:
            Tuple[int, float, str]: The balance_mu amount, last usage time and balance_mu in scientific notation.
        """
        result = self._execute('''
            SELECT balance_mu, last_usage FROM clients WHERE id = ?
        ''', (client_id,))
        row = result.fetchone()
        if row:
            try:
                balance_mu = int(Decimal(str(row['balance_mu'])))
            except (ValueError, InvalidOperation):
                logger.LOGGER(f'Invalid balance_mu value for client {client_id}: {row["balance_mu"]}')
                return None
            return (
                balance_mu,
                row['last_usage'],
                # Third element is the human-readable form, in the operator's
                # display unit: whoever prints this shows it to a person.
                format_mu(balance_mu),
            )
                
        log.LOGGER(f'Client not found: {client_id}')
        return None

    def delete_client(self, client_id: str):
        """Deletes a client from the database."""
        self._execute('''
            DELETE FROM clients WHERE id = ?
        ''', (client_id,))

    def add_balance(self, client_id: str, balance_mu: int = 0):
        """
        Adds balance_mu to a client's balance.

        Args:
            client_id (str): The ID of the client.
            balance_mu (int): The amount of balance_mu to add.
        """
        current, _last_usage, _ = self.get_client_balance(client_id)
        total = current + balance_mu
        # A funded client is an active one: topping up past the threshold clears the
        # expiry clock.
        if _last_usage and total >= CLIENT_MIN_BALANCE_MU_TO_RESET_EXPIRATION:
            _last_usage = None
        self.__update_client(client_id, total, _last_usage)

    def reduce_balance(self, client_id: str, balance_mu: int):
        """
        Reduces balance_mu from a client's balance.

        Args:
            client_id (str): The ID of the client.
            balance_mu (int): The amount of balance_mu to reduce.
        """
        _gas, _last_usage, _ = self.get_client_balance(client_id)
        total_gas = _gas - balance_mu
        if total_gas == 0 and _last_usage is None:
            _last_usage = time.time()
        self.__update_client(client_id, total_gas, _last_usage)

    def client_expired(self, client_id: str) -> bool:
        """
        Checks if a client has expired.

        Args:
            client_id (str): The ID of the client.

        Returns:
            bool: True if the client has expired, False otherwise.
        """
        _gas, _last_usage, _ = self.get_client_balance(client_id)
        return _last_usage is not None and ((time.time() - _last_usage) >= CLIENT_EXPIRATION_TIME)

    def __update_client(self, client_id: str, balance_mu: int, last_usage: float):
        """Updates the balance_mu and last usage time for a client."""
        balance_mu = str(balance_mu)
        self._execute('''
            UPDATE clients SET balance_mu = ?, last_usage = ? WHERE id = ?
        ''', (balance_mu, last_usage, client_id))

    def get_balance_by_client_id(self, id: str) -> int:
        """
        Retrieves the balance_mu amount for a client ID.

        Args:
            id (str): The client ID.

        Returns:
            int: The balance_mu amount.
        """
        result = self._execute('''
            SELECT balance_mu FROM clients WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        if row:
            return int(row['balance_mu'])
        raise Exception(f'Balance not found for ID: {id}')

    # Local instance Methods

    def add_local_instance(
        self,
        father_id: str,
        container_ip: str,
        container_id: str,
        name: str,
        balance_mu: int,
        serialized_instance: str,
        service_id: str,
        virtualizer: str,
        disk_space: int,
        envs: str,
    ):
        """
        Adds an internal container to the database.

        Args:
            father_id (str): The father ID.
            container_ip (str): The IP address of the container.
            container_id (str): The container ID.
            name (str): Friendly instance name.
            balance_mu (int): The balance_mu amount.
            serialized_instance (str): Serialized celaut instance
            service_id (str): Service id
            virtualizer (Optional[str]): Virtualizer backend name
            disk_space (Optional[int]): Disk resource limit for the instance
            envs (Optional[str]): JSON object of the environment variables the
                instance was launched with (e.g. signer mode/seed for a
                source-application), so the node can later tell how it was configured.
        """
        self._execute('''
            INSERT INTO local_instances (id, name, ip, father_id, balance_mu, mem_limit, disk_space, serialized_instance, service_id, virtualizer, envs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (container_id, name, container_ip, father_id, str(balance_mu), 0, disk_space, serialized_instance, service_id, virtualizer, envs))
        log.LOGGER(f'Saved instance {container_id} ({name}) as dependency of {father_id}')

    def get_local_instance_envs(self, id: str) -> Optional[str]:
        """
        Retrieves the stored launch environment variables (raw JSON text) for a
        local instance, or ``None`` when the instance is unknown or was recorded
        without envs.

        Args:
            id (str): The id of the internal container.

        Returns:
            Optional[str]: The JSON-encoded env map, or ``None``.
        """
        cursor = self._execute('''
            SELECT envs FROM local_instances WHERE id = ?
        ''', (id,))
        result = cursor.fetchone()
        return result[0] if result and result[0] is not None else None

    def update_sys_req(
        self,
        id: str,
        mem_limit: Optional[int],
        disk_space: Optional[int] = None,
    ) -> bool:
        """
        Updates system requirements for an internal container.

        Args:
            id (str): The id of the internal container.
            mem_limit (Optional[int]): The new memory limit.
            disk_space (Optional[int]): The new disk limit.

        Returns:
            bool: True if update was successful, False otherwise.
        """
        try:
            self._execute('''
                UPDATE local_instances SET mem_limit = ?, disk_space = ? WHERE id = ?
            ''', (mem_limit, disk_space, id))
            return True
        except:
            return False

    def get_internal_father_id(self, id: str) -> str:
        """
        Retrieves the father_id of an internal container.

        Args:
            id (str): The id of the internal container.

        Returns:
            str: The father_id of the internal container, or an empty string if not found.
        """
        cursor = self._execute('''
            SELECT father_id
            FROM local_instances
            WHERE id = ?
        ''', (id,))
        result = cursor.fetchone()
        return result[0] if result else ""

    def get_internal_instance(self, id: str) -> Optional[str]:
        """
        Retrieves the serialized_instance of an internal container.

        Args:
            id (str): The id of the internal container.

        Returns:
            str: The serialized_instance of the internal container, or None if not found.
        """
        cursor = self._execute('''
            SELECT serialized_instance
            FROM local_instances
            WHERE id = ?
        ''', (id,))
        result = cursor.fetchone()
        return result[0] if result else None

    def get_internal_ip(self, id: str) -> Optional[str]:
        """
        Retrieves the IP address of an internal container.

        Args:
            id (str): The id of the internal container.

        Returns:
            Optional[str]: The IP address of the internal container, or None if not found.
        """
        cursor = self._execute('''
            SELECT ip
            FROM local_instances
            WHERE id = ?
        ''', (id,))
        result = cursor.fetchone()
        return result[0] if result else None

    def get_internal_name(self, id: str) -> Optional[str]:
        result = self._execute('''
            SELECT name
            FROM local_instances
            WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        return row['name'] if row else None

    def get_sys_req(self, id: str) -> dict:
        """
        Retrieves system requirements for an internal container.

        Args:
            id (str): The id of the internal container.

        Returns:
            dict: A dictionary containing the system requirements.
        """
        result = self._execute('''
            SELECT mem_limit, disk_space FROM local_instances WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        if row:
            return row
        raise Exception(f'Internal service {id}')

    def get_instance_balance(self, id: str) -> int:
        """
        Retrieves the balance_mu amount for an internal container.

        Args:
            id (str): The id of the internal container.

        Returns:
            int: The balance_mu amount.
        """
        result = self._execute('''
            SELECT balance_mu FROM local_instances WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        if row:
            return int(row['balance_mu'])
        raise Exception(f'Internal service {id}')
    
    def get_service_id_by_container_id(self, id: str) -> str:
        """
        Retrieves the service ID for a given container ID.

        Args:
            id (str): The container ID.

        Returns:
            str: The associated service ID.

        Raises:
            Exception: If no service is found for the given container ID.
        """
        result = self._execute('''
            SELECT service_id FROM local_instances WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        if row:
            return row['service_id']
        raise Exception(f'No service found for container ID {id}')

    def get_internal_virtualizer(self, id: str) -> Optional[str]:
        """
        Retrieves the virtualizer for a given container ID.

        Args:
            id (str): The container ID.

        Returns:
            Optional[str]: The associated virtualizer, or None if not found.
        """
        result = self._execute('''
            SELECT virtualizer FROM local_instances WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        return row['virtualizer'] if row else None

    def get_all_internal_containers_ids(self) -> List[str]:
        """
        Fetches all ids of internal containers.

        Returns:
            List[str]: A list of ids.
        """
        result = self._execute('''
            SELECT father_id, ip, id FROM local_instances
        ''')
        return [row['id'] for row in result.fetchall()]

    def local_instance_name_exists(self, name: str) -> bool:
        result = self._execute('''
            SELECT COUNT(*)
            FROM local_instances
            WHERE name = ?
        ''', (name,))
        return result.fetchone()[0] > 0

    def update_instance_balance(self, id: str, balance_mu: int):
        """
        Updates the balance_mu amount for a container.

        Args:
            id (str): The id of the container.
            balance_mu (int): The new balance_mu amount.
        """
        
        balance_mu = str(balance_mu)
        self._execute('''
            UPDATE local_instances SET balance_mu = ? WHERE id = ?
        ''', (balance_mu, id))

    def spend_instance_balance(self, id: str, amount_mu: int, allow_debt: bool) -> Optional[bool]:
        """Atomically deduct ``amount_mu`` from a container's balance.

        The read, the sufficiency check and the write happen under a single hold
        of the connection lock, so two threads billing the same instance (e.g.
        the two directions of a service tunnel) can never both read the same
        balance and clobber each other's deduction — the lost-update race that a
        separate ``get_instance_balance`` + ``update_instance_balance`` allows.

        Returns True when the balance_mu was spent, False when the balance is
        insufficient and debt is not allowed, and None when the container does
        not exist. ``balance_mu`` is stored as TEXT, so the arithmetic is done in
        Python (``int``) rather than in SQL to avoid affinity surprises.
        """
        amount_mu = int(amount_mu)
        with SQLConnection._lock:
            try:
                cursor = SQLConnection._connection.cursor()
                cursor.execute('SELECT balance_mu FROM local_instances WHERE id = ?', (id,))
                row = cursor.fetchone()
                if row is None:
                    return None
                current = int(row['balance_mu'])
                if current < amount_mu and not allow_debt:
                    return False
                cursor.execute(
                    'UPDATE local_instances SET balance_mu = ? WHERE id = ?',
                    (str(current - amount_mu), id),
                )
                SQLConnection._connection.commit()
                return True
            except sqlite3.Error as e:
                SQLConnection._connection.rollback()
                raise e

    def internal_instance_exists(self, id: str) -> bool:
        """
        Checks if a internal instance exists in the database.

        Args:
            id (str): The id of the container.

        Returns:
            bool: True if the container exists, False otherwise.
        """
        result = self._execute('''
            SELECT COUNT(*)
            FROM local_instances
            WHERE id = ?
        ''', (id,))
        return result.fetchone()[0] > 0

    def get_local_instance_id_by_name(self, name: str) -> Optional[str]:
        result = self._execute('''
            SELECT id FROM local_instances WHERE name = ?
        ''', (name,))
        row = result.fetchone()
        if row:
            return row['id']

    def resolve_local_instance_reference(self, reference: str) -> Optional[str]:
        if self.internal_instance_exists(id=reference):
            return reference
        resolved_by_name = self.get_local_instance_id_by_name(name=reference)
        if resolved_by_name:
            return resolved_by_name
        return None

    def purge_internal(self, id: str):
        """
        Purges an internal container

        Args:
            id (str): The id of the internal container.

        """
        self._execute('''
            DELETE FROM local_instances WHERE id = ?
        ''', (id,))

    # Peer Methods

    def update_reputation_peer(self, peer_id: str, amount: int) -> bool:
        """
        Updates the reputation of a peer by increasing the reputation score and index.

        Args:
            peer_id (str): The ID of the peer whose reputation is to be updated.
            amount (int): The amount to add to the reputation score.

        Returns:
            bool: True if the update was successful, False otherwise.
        """
        try:
            # Fetch current reputation score and index
            result = self._execute('SELECT reputation_score, reputation_index FROM peer WHERE id = ?', (peer_id,))
            row = result.fetchone()

            if row:
                current_score = row['reputation_score'] or 0  # Handle potential NULL values
                current_index = row['reputation_index'] or 0

                # Update the reputation score and index
                new_score = current_score + amount
                new_index = current_index + 1

                self._execute('''
                    UPDATE peer SET reputation_score = ?, reputation_index = ? WHERE id = ?
                ''', (new_score, new_index, peer_id))

                return True
            else:
                raise Exception(f'Peer not found: {peer_id}')
        except Exception as e:
            logger.LOGGER(f'Error updating reputation for peer {peer_id}: {e}')
            return False

    def get_reputation(self, peer_id: str) -> Optional[float]:
        """
        Retrieves the reputation score for a peer, adjusted by the reputation index.

        Args:
            peer_id (str): The ID of the peer whose reputation is to be retrieved.

        Returns:
            Optional[float]: The adjusted reputation score, or None if the peer is not found.
        """
        try:
            # Fetch current reputation score and index
            result = self._execute('SELECT reputation_score, reputation_index FROM peer WHERE id = ?', (peer_id,))
            row = result.fetchone()

            if row:
                reputation_score = row['reputation_score'] or 0  # Handle potential NULL values
                reputation_index = row['reputation_index'] or 1  # Default to 1 to avoid division by zero

                # Calculate the adjusted reputation score
                adjusted_reputation = reputation_score * (1 + math.log(reputation_index))

                return adjusted_reputation
            else:
                raise Exception(f'Peer not found: {peer_id}')
        except Exception as e:
            logger.LOGGER(f'Error fetching reputation for peer {peer_id}: {e}')
            return None
        
    def total_peer_reputation(self) -> float:
        """
        Fetch the total sum of the reputation of all the peers
        """
        total_amount_result = self._execute('SELECT SUM(reputation_score) AS total_amount FROM peer')
        total_amount_row = total_amount_result.fetchone()
        total_amount = total_amount_row['total_amount'] or 0
        return total_amount

    def submit_to_ledger(self, submit: Callable[[List[Tuple[str, int, str]]], bool], force_submit: bool = False) -> bool:
        """
        Submits the reputation data of all peers to the ledger if the condition
        (reputation_index - last_index_on_ledger > LEDGER_REPUTATION_SUBMISSION_THRESHOLD) is met.

        Args:
            submit (Callable[[List[Tuple[str, int, str]]], bool]): A function that submits the peer's reputation data
                to the ledger. It takes a list of tuples where the first element is the reputation_proof_id (str),
                the second element is the amount (int), and the third element is the peer's instance in JSON format (str).

        Returns:
            bool: True if the submission was successful, False otherwise.
        """

        try:
            # Fetch all peers' data along with their URIs in one query
            result = self._execute('''
                SELECT
                    p.id,
                    p.reputation_proof_id,
                    p.reputation_score,
                    p.reputation_index,
                    p.last_index_on_ledger,
                    p.advertisement,
                    u.ip,
                    u.port
                FROM peer p
                -- Joining uri table to get the addresses this peer announced
                LEFT JOIN uri u ON u.peer_id = p.id
            ''')

            rows = result.fetchall()

            if not rows and not force_submit:
                return True

            # Fetch the total sum of all reputation amounts from the table
            total_amount = self.total_peer_reputation()

            # Dictionary to store the object published for each peer
            peers_dict = {}
            for row in rows:
                peer_id = row['id']
                if peer_id not in peers_dict:
                    # A peer that handshaked with an identity gave us a signed Peer.
                    # Publishing it verbatim keeps that signature verifiable straight
                    # off the ledger, so a reader can check the claim without ever
                    # contacting the peer -- the same property the node's own R9 has.
                    peer_msg = celaut_pb2.Peer()
                    stored = False
                    if row['advertisement']:
                        try:
                            peer_msg.ParseFromString(row['advertisement'])
                            stored = True
                        except Exception as e:
                            logger.LOGGER(f'Peer {peer_id} has an unreadable advertisement: {e}')
                            peer_msg = celaut_pb2.Peer()

                    # Republish only a signature we ourselves verified. The stored blob
                    # is whatever the peer last sent, and a message that failed
                    # verification can still carry a non-empty `signature` field --
                    # trusting that field alone would publish a forgery on-chain under
                    # the "verifiable against the peer's key" promise.
                    if peer_msg.signature and peer_msg.public_key != peer_id:
                        peer_msg.ClearField('signature')
                        peer_msg.ClearField('public_key')

                    peers_dict[peer_id] = {
                        'peer': peer_msg,
                        # An address list rebuilt from our own rows would not match what
                        # the peer signed, so only fill it in when we have no stored
                        # message at all (a peer migrated from before advertisements).
                        'needs_addresses': not stored,
                        'reputation_proof_id': row['reputation_proof_id'],
                        'reputation_score': row['reputation_score'] or 0,
                        'reputation_index': row['reputation_index'] or 0,
                        'last_index_on_ledger': row['last_index_on_ledger'] or 0
                    }

                entry = peers_dict[peer_id]
                if entry['needs_addresses'] and row['ip'] and row['port']:
                    entry['peer'].uri.add(ip=row['ip'], port=row['port'])

            # List to hold data for peers that need to be submitted to the ledger
            needs_submit = force_submit

            if peers_dict:  # If peers are found
                logger.LOGGER(f'Peers found in the database: {peers_dict.keys()}')
                to_submit = []
                token_amount = TOTAL_REPUTATION_TOKEN_AMOUNT -1  # Subtract 1 to account for the node instance

                for peer_id, data in peers_dict.items():
                    reputation_proof_id = data['reputation_proof_id']
                    reputation_score = data['reputation_score']
                    reputation_index = data['reputation_index']
                    last_index_on_ledger = data['last_index_on_ledger']

                    if reputation_proof_id:
                        # Convert the peer object to JSON string
                        instance_json = MessageToJson(data['peer'])

                        # Calculate the percentage of the total reputation token amount
                        if reputation_index - last_index_on_ledger >= env_manager.get("ledgers.ergo.reputation.LEDGER_REPUTATION_SUBMISSION_THRESHOLD"):
                            logger.LOGGER(f'Peer {peer_id} with proof {reputation_proof_id} meets the submission threshold.')
                            needs_submit = True
                            percentage_amount = ((reputation_score / total_amount) * token_amount) if total_amount else 0
                            to_submit.append((reputation_proof_id, percentage_amount, instance_json))

                        # Proof percentage doesn't need to be changed itself, but needs to be updated if others do.
                        elif last_index_on_ledger > 0:
                            logger.LOGGER(f'Peer {peer_id} with proof {reputation_proof_id} does not meet the submission threshold, but is included in the proof.')
                            percentage_amount = ((reputation_score / total_amount) * token_amount) if total_amount else 0
                            to_submit.append((reputation_proof_id, percentage_amount, instance_json))

                to_submit.append((None, 1, None))  # This will be treated as a pointer to itself, used to include the node instance in the proof

            else:  # If no peers are found, submit the total amount of reputation tokens, but only if force_submit is True
                logger.LOGGER('No peers found in the database.')
                to_submit = [(None, TOTAL_REPUTATION_TOKEN_AMOUNT, None)]


            # Attempt to submit the data to the ledger
            if needs_submit and to_submit:
                success = submit(to_submit)
                if success:
                    logger.LOGGER('Reputation proofs submitted successfully.')
                    # Update the last index on ledger for all submitted peers
                    for peer_id, data in peers_dict.items():
                        reputation_proof_id = data['reputation_proof_id']
                        if reputation_proof_id and any(reputation_proof_id == _e[0] for _e in to_submit):
                            self._execute('UPDATE peer SET last_index_on_ledger = ? WHERE id = ?', (data['reputation_index'], peer_id))
                    return True
                else:
                    logger.LOGGER('Failed to submit to ledger for some or all peers.')
                    return False
            else:
                return True

        except Exception as e:
            logger.LOGGER(f'Error submitting to ledger: {e}')
            return False

    @staticmethod
    def ledger_key(ledger: Any) -> str:
        """The ``hash`` value the ledger table is keyed by, from a Ledger or a hash.

        Callers on the payment path hold the deserialized ``Contract.Ledger``
        message (that is what ``get_peer_contract_instances`` yields), so derive
        the digest the same way ``add_contract`` does when it stores the row.
        """
        if isinstance(ledger, str):
            return ledger
        return sha3_256(ledger.SerializeToString()).hexdigest()

    def ledger_hashes(self, ledger: Any) -> List[str]:
        """Stored ``ledger.hash`` values a ledger identifier resolves to.

        The ``ledger`` table is keyed by the sha3 of the serialized
        ``Contract.Ledger`` message, but payment-path callers only hold the ledger
        *tag* (``"ergo"``) — that is all a peer's advertisement carries as a stable
        name. Accept either: an exact stored hash, or a tag to resolve against the
        deserialized rows.
        """
        key = self.ledger_key(ledger)
        rows = self._execute("SELECT hash, content FROM ledger").fetchall()

        exact = [row['hash'] for row in rows if row['hash'] == key]
        if exact:
            return exact

        by_tag: List[str] = []
        for row in rows:
            parsed = celaut_pb2.Contract.Ledger()
            try:
                parsed.ParseFromString(row['content'])
            except Exception as e:
                logger.LOGGER(f'Could not parse stored ledger {row["hash"]}: {e}')
                continue
            if key in parsed.tags:
                by_tag.append(row['hash'])
        return by_tag

    def update_double_attempt_retry_time_on_ledger(self, ledger: Any):
        """
        Updates the double_spending_retry_time field in the ledger table
        by setting it to the current time plus 10 minutes for the specified ledger.

        Args:
            ledger: The ledger whose retry_time needs updating, as a
                ``Contract.Ledger`` message or as its ``hash``.
        """
        query = """
        UPDATE ledger
        SET double_spending_retry_time = DATETIME('now', '+10 minutes')
        WHERE hash = ?
        """

        self._execute(query, (self.ledger_key(ledger),))

    def check_if_ledger_is_available(self, ledger: Any) -> bool:
        """
        Checks if the specified ledger is available for use.
        A ledger is considered available if its double_spending_retry_time is NULL
        or is in the past.

        Args:
            ledger: The ledger to check, as a ``Contract.Ledger`` message or as
                its ``hash``.

        Returns:
            bool: True if the ledger is available, False otherwise.
        """
        query = """
        SELECT double_spending_retry_time
        FROM ledger
        WHERE hash = ?
        """

        # Execute the query to get the retry time for the specified ledger.
        result = self._execute(query, (self.ledger_key(ledger),)).fetchone()

        # Check if a result was returned and evaluate its availability.
        if result:
            retry_time = result[0]

            # A ledger is available if the retry_time is NULL or in the past.
            if retry_time is None or retry_time < datetime.utcnow().isoformat():
                return True

        return False

    def get_peers(self) -> List[dict]:
        """
        Fetches all peers from the database.

        Returns:
            List[dict]: A list of dictionaries containing peer details.
        """
        result = self._execute('''
            SELECT id, token, remote_client_id, balance_mu FROM peer
        ''')

        peers = []
        for row in result.fetchall():
            peer = dict(row)
            peer['balance_mu'] = int(peer.pop('balance_mu'))
            peers.append(peer)

        return peers

    def get_peer_by_id(self, peer_id: str) -> dict:
        """
        Fetches details of a peer by its ID from the database.

        Parameters:
        - peer_id (str): The unique identifier of the peer.

        Returns:
        - dict: A dictionary containing peer details if found, otherwise an empty dictionary.
        """
        try:
            # Execute SQL query to retrieve peer details by ID
            result = self._execute('SELECT * FROM peer WHERE id = ?', (peer_id,))
            row = result.fetchone()

            if row:
                # Convert the row to a dictionary
                peer_info = dict(row)
                # The balance is stored as TEXT and is an integer everywhere else (Python ints
                # are unbounded; a float both loses precision and re-serializes as
                # "1e+64", which `from_amount` cannot parse back).
                peer_info['balance_mu'] = int(Decimal(str(peer_info.pop('balance_mu') or 0)))
                return peer_info
            else:
                return {}  # Return empty dict if peer not found
        except Exception as e:
            logger.LOGGER(f'Error fetching peer details for ID {peer_id}: {e}')
            return {}
        
    def get_peer_contract_rate(self, peer_id: str, contract_hash: str, ledger_hash: str) -> Optional[int]:
        """
        Fetches the balance_mu price for a specific contract instance, identified by
        peer, contract hash, and ledger ID.

        Parameters:
        - peer_id (str): The unique identifier of the peer.
        - contract_hash (str): The hash of the contract.
        - ledger_hash (str): The ledger, either as its stored ``ledger.hash`` or as
          a ledger tag such as ``"ergo"`` (see ``ledger_hashes``). Callers on the
          payment path only hold the tag, so matching the column verbatim would
          never find the row the peer's advertisement created.

        Returns:
        - int: The balance_mu price as an integer if the specific contract instance is found.
        - None: If the specific contract instance is not found or an error occurs.
        """
        try:
            for stored_hash in self.ledger_hashes(ledger_hash):
                result = self._execute('''
                    SELECT mu_per_unit
                    FROM contract_instance
                    WHERE peer_id = ? AND contract_hash = ? AND ledger_hash = ?
                ''', (peer_id, contract_hash, stored_hash))

                # Fetch one row (we expect at most one for this combination)
                row = result.fetchone()
                if not row:
                    continue

                gas_price_str = row['mu_per_unit']
                try:
                    # Convert the string mu_per_unit to an integer
                    return int(gas_price_str)
                except (ValueError, TypeError) as ve:
                    logger.LOGGER(f'Error converting stored mu_per_unit "{gas_price_str}" to int for instance: peer={peer_id}, contract={contract_hash}, ledger={ledger_hash}. Error: {ve}')
                    return None # Return None if conversion fails

            # No row found for the given criteria
            return None # Indicate that the specific instance was not found

        except Exception as e:
            # Catch potential database errors during execution
            logger.LOGGER(f'Database error fetching balance_mu price for instance: peer={peer_id}, contract={contract_hash}, ledger={ledger_hash}. Error: {e}')
            return None # Return None on database error

    def get_peer_payment_contracts(self, peer_id: str) -> List[dict]:
        """
        Lists every payment contract instance registered for a peer, resolving
        each row's ledger to its tag (e.g. ``"ergo"``) instead of the opaque
        stored hash.

        Args:
            peer_id (str): The peer id (``"LOCAL"`` for our own contracts).

        Returns:
            List[dict]: One entry per ``contract_instance`` row, each with
            ``contract_hash``, ``ledger_tag`` (falling back to the raw
            ``ledger_hash`` if the ledger content can't be resolved to a tag),
            ``address``, and ``mu_per_unit`` (int, or None if unset/invalid).
        """
        rows = self._execute(
            "SELECT contract_hash, ledger_hash, address, mu_per_unit "
            "FROM contract_instance WHERE peer_id = ?",
            (peer_id,)
        ).fetchall()

        contracts = []
        for row in rows:
            ledger_tag = row['ledger_hash']
            ledger_row = self._execute(
                "SELECT content FROM ledger WHERE hash = ?", (row['ledger_hash'],)
            ).fetchone()
            if ledger_row:
                ledger = celaut_pb2.Contract.Ledger()
                try:
                    ledger.ParseFromString(ledger_row['content'])
                    if ledger.tags:
                        ledger_tag = ledger.tags[0]
                except Exception as e:
                    logger.LOGGER(f'Could not parse stored ledger {row["ledger_hash"]}: {e}')

            try:
                mu_per_unit = int(row['mu_per_unit'])
            except (TypeError, ValueError):
                mu_per_unit = None

            contracts.append({
                'contract_hash': row['contract_hash'],
                'ledger_tag': ledger_tag,
                'address': row['address'],
                'mu_per_unit': mu_per_unit,
            })

        return contracts

    def get_peers_id(self) -> List[str]:
        """
        Fetches all peer IDs from the database.

        Returns:
            List[str]: A list of peer IDs.
        """
        try:
            result = self._execute("SELECT id FROM peer")
            peer_ids = [row[0] for row in result.fetchall()]
            return peer_ids
        except sqlite3.Error as e:
            logger.LOGGER(f'Error fetching peer IDs: {e}')
            return []

    def add_balance_to_peer(self, peer_id: str, balance_mu: int) -> bool:
        """
        Adds the specified amount of balance_mu to the existing balance_mu value of a peer.

        Parameters:
        - peer_id (str): The unique identifier of the peer.
        - balance_mu (int): The amount of balance_mu to be added to the peer's existing balance_mu.

        Returns:
        - bool: True if the operation was successful, False otherwise.
        """
        try:
            # Retrieve the current balance_mu values from the database.
            result = self._execute('SELECT balance_mu FROM peer WHERE id = ?', (peer_id,))
            row = result.fetchone()

            if row:
                current_gas = int(row['balance_mu'])

                # Add the specified balance_mu to the current amount.
                total_gas = str(current_gas + balance_mu)

                # Get the current timestamp for balance_last_update.
                current_time = datetime.datetime.now().isoformat()

                # Update the peer's balance_mu values and balance_last_update in the database.
                self._execute('''
                    UPDATE peer SET balance_mu = ?, balance_last_update = ? WHERE id = ?
                ''', (total_gas, current_time, peer_id))

                return True
            else:
                raise Exception(f'Peer not found: {peer_id}')
        except Exception as e:
            logger.LOGGER(f'Error adding balance_mu to peer {peer_id}: {e}')
            return False

    def refresh_balance_for_peer(self, peer_id: str, balance_mu: int) -> bool:
        """
        Sets the balance_mu value of a peer to a specified amount, replacing any existing value.

        Parameters:
        - peer_id (str): The unique identifier of the peer.
        - balance_mu (int): The new balance_mu amount to set for the peer.

        Returns:
        - bool: True if the operation was successful, False otherwise.
        """
        try:
            balance_mu = str(balance_mu)

            # Get the current timestamp for balance_last_update.
            current_time = datetime.datetime.now().isoformat()

            # Update the peer's balance_mu and balance_last_update directly in the database.
            self._execute('''
                UPDATE peer SET balance_mu = ?, balance_last_update = ? WHERE id = ?
            ''', (balance_mu, current_time, peer_id))

            return True
        except Exception as e:
            logger.LOGGER(f'Error refreshing balance_mu for peer {peer_id}: {e}')
            return False

    def add_peer(self, peer_id: str, advertisement: bytes) -> bool:
        """
        Adds a peer to the database.

        Args:
            peer_id (str): The ID of the peer to add.
            advertisement (bytes): Serialized ``celaut.Peer`` holding what the peer
                declares node-wide (payment contracts and rates). Its addresses are
                stored separately, one ``uri`` row each.

        Returns:
            bool: True if the peer was successfully added, False otherwise.
        """
        logger.LOGGER(f'Attempting to add peer {peer_id}')

        if not self.peer_exists(peer_id=peer_id):
            try:
                self._execute('''
                    INSERT INTO peer (id, advertisement, remote_client_id, balance_mu)
                    VALUES (?, ?, '', '0')  -- Initialize with empty remote_client_id and 0 balance_mu
                ''', (peer_id, advertisement))
                logger.LOGGER(f'Peer {peer_id} added')
                return True
            except sqlite3.Error as e:
                logger.LOGGER(f'Failed to add peer {peer_id}: {e}')
                return False
        else:
            logger.LOGGER(f'Peer {peer_id} already exists')
            return False

    def set_peer_advertisement(self, peer_id: str, advertisement: bytes) -> None:
        """Refresh what a known peer declares node-wide (payment contracts, rates)."""
        self._execute(
            "UPDATE peer SET advertisement = ? WHERE id = ?", (advertisement, peer_id)
        )

    def get_peer_advertisement(self, peer_id: str) -> Optional[bytes]:
        """The peer's last stored node-wide declaration, or None if it has none."""
        row = self._execute(
            "SELECT advertisement FROM peer WHERE id = ?", (peer_id,)
        ).fetchone()
        return bytes(row[0]) if row and row[0] else None

    def add_peer_uri(self, uri: celaut_pb2.Peer.Uri, peer_id: str, transport: str):
        """
        Adds or merges one of a peer's addresses into the database.

        Upserts on (peer_id, ip, port): inserted if new, refreshed otherwise, instead
        of always inserting a fresh row. This makes re-registering an already-known
        peer (a reconnect, a pay-time refresh, a re-introduction) idempotent by
        construction, and lets a peer accumulate several reachable addresses over time
        instead of losing every one but the last it happened to advertise (issue #236).

        Args:
            uri (celaut_pb2.Peer.Uri): The address to add.
            peer_id (str): The ID of the peer that announced it.
            transport (str): Host-supported transport this address speaks ("tcp"/"udp"),
                resolved from ``uri.transport`` by the caller.
        """
        # Single atomic upsert against idx_uri_peer_ip_port. A SELECT-then-INSERT would
        # race: _execute commits per statement, so the lock is released in between and
        # two concurrent IntroducePeer threads would both insert.
        protocol_stack = celaut_pb2.Peer.Uri()
        protocol_stack.protocol_stack.extend(uri.protocol_stack)
        self._execute(
            "INSERT INTO uri (peer_id, ip, port, expiry_unix_timestamp, transport, protocol_stack) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (peer_id, ip, port) DO UPDATE SET "
            "expiry_unix_timestamp = excluded.expiry_unix_timestamp, "
            "transport = excluded.transport, "
            "protocol_stack = excluded.protocol_stack",
            (
                peer_id,
                uri.ip,
                uri.port,
                int(uri.expiry_unix_timestamp) or None,
                transport,
                protocol_stack.SerializeToString(),
            ),
        )

    def check_if_ledger_exists(self, ledger_to_check: celaut_pb2.Contract.Ledger) -> celaut_pb2.Contract.Ledger:
        """
        Checks if a logically equivalent ledger already exists in the database.

        This method defines an inner comparison function to determine ledger equivalence
        by prioritizing the 'formal' and 'prose' fields over 'tags'. It then iterates
        through all ledgers in the 'ledger' table, deserializes them, and uses this
        comparison logic.

        Args:
            ledger_to_check: The Ledger object to check for.

        Returns:
            The complete Ledger object from the database if a match is found.  The same if not exists.
        """
        
        def _compare_ledgers(ledger_a: celaut_pb2.Contract.Ledger, ledger_b: celaut_pb2.Contract.Ledger) -> bool:
            """
            Inner function to compare two Ledger objects for logical equivalence.
            """
            if not ledger_a or not ledger_b:
                return False

            # 1. Highest priority: the 'formal' field. It's the strictest identifier.
            if ledger_a.formal and ledger_b.formal and ledger_a.formal == ledger_b.formal:
                return True

            # 2. Second priority: the 'prose' field.
            if ledger_a.prose and ledger_b.prose and ledger_a.prose == ledger_b.prose:
                return True

            # 3. If neither formal nor prose are present, check for common tags.
            #    If there's at least one common tag, they match.
            if not ledger_a.prose and not ledger_b.prose and not ledger_a.formal and not ledger_b.formal:
                # Convert tag lists to sets for efficient intersection checking
                tags_a_set = set(ledger_a.tags)
                tags_b_set = set(ledger_b.tags)
                
                # Check if the intersection of the two sets is not empty
                if tags_a_set.intersection(tags_b_set):
                    return True
                
            return False

        # Fetch all stored ledgers. 'content' holds the serialized ledger; 'hash' is
        # only its sha3 digest, and there is no 'id' column at all (see migrate.py).
        cursor = self._execute("SELECT content FROM ledger")

        for row in cursor.fetchall():
            # The ledger from the DB is in a serialized byte format.
            db_ledger_bytes = row['content']
            
            # Deserialize the bytes to reconstruct the Ledger object.
            db_ledger = celaut_pb2.Contract.Ledger()
            db_ledger.ParseFromString(db_ledger_bytes)
            
            # Use the inner comparison logic.
            if _compare_ledgers(ledger_to_check, db_ledger):
                # If they match, return the full object from the database.
                return db_ledger
        
        # If the loop finishes without finding a match, return the same.
        return ledger_to_check

    def add_contract(self, contract: celaut_pb2.Contract, peer_id: str = "LOCAL", mu_per_unit: int = 0):
        """
        Adds a contract to the database.

        Args:
            contract (celaut_pb2.Contract): The contract to add.
            peer_id (Optional[str]): The ID of the peer or None for a self contract (to be send to clients.)
            mu_per_unit (Int): MU that one unit of this contract is worth.
        """
        # The per-instance value is the raw ErgoTree/propositionBytes (script xattr). It is
        # stored as hex so it round-trips as binary, never as a textual address. The
        # contract_hash keys on the STABLE, wallet-independent contract_type so the same
        # kind of contract matches across nodes (falling back to the script/shape when no
        # explicit type is advertised, e.g. the simulator).
        raw_script: bytes = get_script(contract)
        type_bytes: bytes = get_contract_type(contract) or raw_script or contract_shape_bytes(contract)
        instance_value: str = raw_script.hex() if raw_script else get_address(contract)

        ledger = self.check_if_ledger_exists(ledger_to_check=contract.ledger)
        ledger_str: bytes = ledger.SerializeToString()

        contract_hash: str = sha3_256(type_bytes).hexdigest()
        ledger_hash: str = sha3_256(ledger_str).hexdigest()

        gas_str = str(mu_per_unit)

        self._execute("INSERT OR IGNORE INTO contract (hash, content) VALUES (?,?)",
                    (contract_hash, type_bytes))

        self._execute("INSERT OR IGNORE INTO ledger (hash, content) VALUES (?,?)",
                    (ledger_hash, ledger_str))

        self._execute("INSERT OR IGNORE INTO contract_instance (address, ledger_hash, contract_hash, peer_id, mu_per_unit) "
                    "VALUES (?,?,?,?,?)", (instance_value, ledger_hash, contract_hash, peer_id, gas_str))

    def get_peer_contract_instances(self, contract_hash: str, peer_id: str = "LOCAL") -> Generator[Tuple[bytes, celaut_pb2.Contract.Ledger], None, None]:
        """
        Retrieves all contract instances for a given contract hash and peer ID.

        Args:
            contract_hash (str): Contract hash
            peer_id (str): Peer ID, defaults to "LOCAL".

        Yields:
            Tuple[bytes, celaut_pb2.Contract.Ledger]: A tuple containing the address and the ledger of the contract instance.
        """
        cursor = self._execute(
            "SELECT address, ledger_hash FROM contract_instance WHERE contract_hash = ? AND peer_id = ?",
            (contract_hash, peer_id)
        )
        for row in cursor.fetchall():
            cursor = self._execute("SELECT content FROM ledger WHERE hash = ?", (row['ledger_hash'],))
            ledger_str = cursor.fetchone()['content']

            ledger = celaut_pb2.Contract.Ledger()
            ledger.ParseFromString(ledger_str)

            stored = row['address'] or ""
            try:
                # Ergo instances store the raw ErgoTree/propositionBytes as hex.
                script: bytes = bytes.fromhex(stored)
            except (ValueError, TypeError):
                # Legacy/simulator instances stored a textual value.
                script = stored.encode('utf-8')

            yield script, ledger

    def add_reputation_proof(self, contract: celaut_pb2.Contract, peer_id: str) -> bool:
        """
        Add or update the reputation_proof_id for a peer.

        Args:
            peer_id (str): The ID of the peer whose reputation_proof_id is to be updated.
            contract (celaut_pb2.Contract): The reputation proof contract ledger.

        Returns:
            bool: True if the update was successful, False otherwise.
        """

        try:
            new_proof_id = get_token_id(contract)

            # Fetch the peer to ensure it exists
            result = self._execute('SELECT id FROM peer WHERE id = ?', (peer_id,))
            row: Any = result.fetchone()

            if row:
                # Update the reputation_proof_id for the peer
                self._execute('''
                    UPDATE peer SET reputation_proof_id = ? WHERE id = ?
                ''', (new_proof_id, peer_id))

                logger.LOGGER(f'Reputation proof ID updated for peer {peer_id}')
                return True
            else:
                raise Exception(f'Peer not found: {peer_id}')
        except Exception as e:
            logger.LOGGER(f'Error updating reputation_proof_id for peer {peer_id}: {e}')
            return False

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

    def set_forced_execution_peer(self, token: str, peer_id: str) -> None:
        """
        Records that the `StartService` call correlated with ``token`` (its
        ``recursion_guard_token``, set by `nodo force_execution` before opening
        the call) must be delegated straight to ``peer_id``, bypassing
        ``execution_balancer``.

        Keyed by a fresh, single-use token rather than the client id: dev
        client ids are drawn from a small reusable pool (see
        `manager.get_execute_client`), so keying on the client id could leak a
        stale forced-peer hint onto a later, unrelated `nodo execute` call that
        happens to draw the same recycled id.

        Args:
            token (str): The one-time correlation token for this call.
            peer_id (str): The peer to force delegation to.
        """
        self._execute(
            "INSERT OR REPLACE INTO forced_execution_peer (token, peer_id) VALUES (?, ?)",
            (token, peer_id)
        )

    def pop_forced_execution_peer(self, token: str) -> Optional[str]:
        """
        Reads and deletes the forced-peer hint for ``token``, if any.

        Consuming it (rather than just reading it) means the hint can only ever
        apply to the single `launch_service` call it was created for.

        Args:
            token (str): The one-time correlation token to look up.

        Returns:
            Optional[str]: The peer id to force delegation to, or None if no
            hint is recorded for this token.
        """
        row = self._execute(
            "SELECT peer_id FROM forced_execution_peer WHERE token = ?", (token,)
        ).fetchone()
        if not row:
            return None
        self._execute("DELETE FROM forced_execution_peer WHERE token = ?", (token,))
        return row['peer_id']

    def remove_peer(self, peer_id: str) -> bool:
        """
        Removes a peer from the database along with all related records.
        This includes contract_instances and URIs associated with the peer.

        Args:
            peer_id (str): The ID of the peer to remove.

        Returns:
            bool: True if the peer was successfully removed, False otherwise.
        """
        try:
            # Define all deletion queries in proper order
            deletion_queries = [
                # Delete URIs announced by this peer
                ('DELETE FROM uri WHERE peer_id = ?', (peer_id,)),

                # Delete contract instances related to this peer
                ('DELETE FROM contract_instance WHERE peer_id = ?', (peer_id,)),
                
                # Delete the peer itself
                ('DELETE FROM peer WHERE id = ?', (peer_id,))
            ]
            
            # Execute all deletions in a single transaction
            self._execute2(deletion_queries)
            
            logger.LOGGER(f'Peer {peer_id} and all related records removed from the database')
            return True
            
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to remove peer {peer_id}: {e}')
            return False

    def rekey_peer(self, old_peer_id: str, new_peer_id: str) -> bool:
        """Move a peer's whole record from ``old_peer_id`` to ``new_peer_id``.

        The migration path for issue #236: peers registered before node identity
        existed hold a random uuid4 id, and the first time such a peer re-handshakes
        with a valid signature we must adopt its public key as the id *in place* --
        otherwise it registers as a brand-new peer and its balance_mu balance, external
        client id, payment contracts and reputation stay stranded on an orphaned row
        that nothing references again.

        ``peer_id`` is a foreign key in uri, contract_instance and delegated_instances,
        so all four tables move together, in one transaction. ``"LOCAL"`` is a reserved
        sentinel for this node's own contracts and is never a discovered peer, so it is
        refused outright.
        """
        if not old_peer_id or not new_peer_id or old_peer_id == new_peer_id:
            return False
        if "LOCAL" in (old_peer_id, new_peer_id):
            logger.LOGGER('Refusing to re-key the reserved LOCAL peer id.')
            return False
        if self.peer_exists(peer_id=new_peer_id):
            logger.LOGGER(f'Cannot re-key {old_peer_id}: {new_peer_id} already exists.')
            return False

        try:
            self._execute2([
                ('UPDATE peer SET id = ? WHERE id = ?', (new_peer_id, old_peer_id)),
                ('UPDATE uri SET peer_id = ? WHERE peer_id = ?', (new_peer_id, old_peer_id)),
                ('UPDATE contract_instance SET peer_id = ? WHERE peer_id = ?', (new_peer_id, old_peer_id)),
                ('UPDATE delegated_instances SET peer_id = ? WHERE peer_id = ?', (new_peer_id, old_peer_id)),
            ])
            logger.LOGGER(f'Re-keyed peer {old_peer_id} to its public key {new_peer_id}.')
            return True
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to re-key peer {old_peer_id} to {new_peer_id}: {e}')
            return False

    def prune_peer_uris(self, peer_id: str, keep: Iterable[Tuple[str, int]]) -> int:
        """Drop every URI of ``peer_id`` that is not in ``keep``; return how many went.

        Merging alone would let a peer's addresses grow without bound and, worse, keep
        a superseded one forever: ``generate_uris_by_peer_id`` yields in insertion
        order, so the *stale* address is the one every ``next(...)`` call site picks
        while anything still answers on it -- exactly the downgrade the signed ``ts``
        anti-replay check exists to prevent, arriving through the back door. Only ever
        driven by a verified, strictly-newer announcement, so an unsigned or replayed
        message can never prune a peer's real addresses.
        """
        kept = {(str(ip), int(port)) for ip, port in keep}
        current = self._execute(
            "SELECT u.id, u.ip, u.port FROM uri u "
            "WHERE u.peer_id = ?",
            (peer_id,),
        ).fetchall()

        stale = [row[0] for row in current if (str(row[1]), int(row[2])) not in kept]
        if not stale:
            return 0
        self._execute2([
            (f"DELETE FROM uri WHERE id IN ({','.join('?' * len(stale))})", tuple(stale)),
        ])
        logger.LOGGER(f'Pruned {len(stale)} superseded URI(s) from peer {peer_id}.')
        return len(stale)

    def get_peer_last_ts(self, peer_id: str) -> Optional[int]:
        """The ``ts`` of the last signed Peer message accepted from ``peer_id``.

        None when the peer has never presented one (a legacy/unsigned peer, or one
        seen for the first time), so the caller's "strictly newer than last accepted"
        anti-replay check has nothing to compare against and lets the message through.
        """
        row = self._execute(
            "SELECT last_ts FROM peer WHERE id = ?", (peer_id,)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def set_peer_last_ts(self, peer_id: str, ts: int) -> None:
        """Record the ``ts`` of the last signed Peer message accepted from ``peer_id``."""
        self._execute(
            "UPDATE peer SET last_ts = ? WHERE id = ?",
            (int(ts), peer_id),
        )

    def get_peer_expiry_unix_timestamp(self, peer_id: str) -> Optional[int]:
        """When the soonest of ``peer_id``'s announced addresses may stop being valid, or None."""
        row = self._execute(
            "SELECT MIN(expiry_unix_timestamp) FROM uri "
            "WHERE peer_id = ? AND expiry_unix_timestamp > 0",
            (peer_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def peer_uris_exist(self, uris) -> bool:
        """True if any of ``uris`` (each exposing ``.ip``/``.port``) is already known.

        Args:
            uris: an iterable of URI-shaped objects (e.g. ``celaut_pb2.Peer.Uri``).

        Returns:
            bool:
                - True if at least one URI exists in the database.
                - True in case of an unexpected error (failsafe behavior).
                - False if no URIs exist in the database or ``uris`` is empty.
        """
        try:
            for uri in uris:
                if self.uri_exists(uri=uri):
                    return True
        except Exception as e:
            logger.LOGGER(f"Error while checking peer URI existence: {e}")
            return True  # Failsafe: Return True to prevent disruption in case of error
        return False

    def uri_exists(self, uri: str|celaut_pb2.Instance.Uri) -> bool:
        """
        Checks if a URI (ip:port) exists in the database.

        Args:
            uri (str | celaut_pb2.Instance.Uri): 
                The URI to check. It can be a string in the format 'ip:port' 
                or an instance of `celaut_pb2.Instance.Uri` with `ip` and `port` attributes.

        Returns:
            bool: True if the URI exists, False otherwise.
        """
        try:
            # Split the URI into IP and port
            if type(uri) is str:
                
                ip, port = uri.split(':')
            else:
                ip, port = uri.ip, uri.port
            port = int(port)  # Convert port to an integer

            # Query the database to check if the IP and port exist
            result = self._execute('''
                SELECT COUNT(*)
                FROM uri
                WHERE ip = ? AND port = ?
            ''', (ip, port))

            return result.fetchone()[0] > 0
        except ValueError:
            # Handle the case where the URI is not in the correct format
            logger.LOGGER(f'Invalid URI format: {uri}. Expected format is "ip:port".')
            return False

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
                UPDATE peer SET remote_client_id = ? WHERE id = ?
            ''', (client_id, peer_id))
            logger.LOGGER(f'Associated external client {client_id} with peer {peer_id}')
            return True
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to associate external client {client_id} with peer {peer_id}: {e}')
            return False

    # The delegated_instances key column is `token_delegation` (the token as the
    # remote peer knows it); `id` holds our hashed alias of it. There is no
    # `token` column, and the payload column is `serialized_instance`.

    def get_external_father_id(self, token: str) -> str:
        """
        Retrieves the father_id of an delegated instance based on the token.

        Args:
            token (str): The token of the delegated instance.

        Returns:
            str: The father_id of the external container, or an empty string if not found.
        """
        cursor = self._execute('''
            SELECT father_id
            FROM delegated_instances
            WHERE token_delegation = ?
        ''', (token,))
        result = cursor.fetchone()
        return result[0] if result else ""

    def get_delegated_instance(self, token: str) -> Optional[str]:
        """
        Retrieves the serialized instance of an external container based on the token.

        Args:
            token (str): The token of the external container.

        Returns:
            str: The serialized instance of the external container, or None if not found.
        """
        cursor = self._execute('''
            SELECT serialized_instance
            FROM delegated_instances
            WHERE token_delegation = ?
        ''', (token,))
        result = cursor.fetchone()
        return result[0] if result else None

    def get_delegated_instances(self) -> List[dict]:
        """
        Fetches every delegated instance, for restoring state on startup.

        Returns:
            List[dict]: token (as the peer knows it), id (our hashed alias),
                peer_id, father_id and the serialized instance.
        """
        result = self._execute('''
            SELECT token_delegation, id, peer_id, father_id, serialized_instance
            FROM delegated_instances
        ''')
        return [
            {
                'token': row[0],
                'id': row[1],
                'peer_id': row[2],
                'father_id': row[3],
                'serialized_instance': row[4],
            }
            for row in result.fetchall()
        ]

    def purgue_delegated(self, token: str):
        """
        Purges an external container

        Args:
            token (str): The token of the external container.

        """
        self._execute('''
            DELETE FROM delegated_instances WHERE token_delegation = ?
        ''', (token,))

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
                SELECT remote_client_id FROM peer WHERE id = ?
            ''', (peer_id,))
            row = result.fetchone()
            if row and row['remote_client_id']:
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
                SELECT remote_client_id FROM peer WHERE id = ?
            ''', (peer_id,))
            row = result.fetchone()
            if row:
                return row['remote_client_id']
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
                UPDATE peer SET remote_client_id = NULL WHERE id = ?
            ''', (peer_id,))
            logger.LOGGER(f'Successfully deleted external client associated with peer {peer_id}')
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to delete external client associated with peer {peer_id}: {e}')
            pass

    def add_delegated_instance(self, father_id: str, encrypted_external_token: str, external_token: str, peer_id: str, serialized_instance: str, service_id: str):
        """
        Adds an external container to the database.

        Args:
            father_id (str): The father ID.
            encrypted_external_token (str): The encrypted external token.
            external_token (str): The external token.
            peer_id (str): The peer ID.
            serialized_instance (str): Serialized celaut instance.
            service_id (str): Service id
        """
        self._execute('''
            INSERT INTO delegated_instances (token_delegation, id, peer_id, father_id, serialized_instance, service_id)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (external_token, encrypted_external_token, peer_id, father_id, serialized_instance, service_id))

    def get_delegated_token_by_id(self, id: str) -> Optional[str]:
        """
        Retrieves the token associated with a given hashed token for an external container.

        Args:
            id (str): The hashed token of the external container.

        Returns:
            Optional[str]: The token if it exists, or None if not found.
        """
        try:
            result = self._execute('''
                SELECT token_delegation FROM delegated_instances WHERE id = ?
            ''', (id,))
            row = result.fetchone()
            if row:
                return row['token_delegation']
            return None
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to retrieve token for hashed external container token {id}: {e}')
            return None

    def get_peer_id_by_external_service(self, token: str) -> Optional[str]:
        """
        Retrieves the peer id where the external container was requested.

        Args:
            token (str): The token of the external container.

        Returns:
            Optional[str]: The peer id if it exists, or None if not found.
        """
        try:
            result = self._execute('''
                SELECT peer_id FROM delegated_instances WHERE token_delegation = ?
            ''', (token,))
            row = result.fetchone()
            if row:
                return row['peer_id']
            return None
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to retrieve peer_id for external container {token}: {e}')
            return None

    def purge_external(self, agent_id: str, peer_id: str, his_token: str) -> int:  # TODO delete?
        """
        Purges an external container and refunds balance_mu.

        Args:
            agent_id (str): The agent ID.
            peer_id (str): The peer ID.
            his_token (str): The token of the external container.

        Returns:
            int: The balance_mu amount refunded.
        """
        refund = 0

        hashed_token = self._execute('''
            SELECT id FROM delegated_instances WHERE token_delegation = ?
        ''', (his_token,)).fetchone()["id"]

        self._execute('''
            DELETE FROM delegated_instances WHERE token_delegation = ?
        ''', (his_token,))

        try:
            refund = from_amount(next(bee.client_grpc(
                method=celaut_pb2_grpc.GatewayStub(
                    grpc.insecure_channel(
                        next(generate_uris_by_peer_id(peer_id=peer_id))
                    )
                ).StopService,
                input=celaut_pb2.TokenMessage(
                    token=hashed_token
                ),
                indices_parser=celaut_pb2.Refund,
                partitions_message_mode_parser=True
            )).amount)
        except grpc.RpcError as e:
            log.LOGGER('Error during remove a container on ' + peer_id + ' ' + str(e))

        return refund

    # Common Methods

    def get_local_instance_id_by_uri(self, uri: str) -> Optional[str]:
        """
        Retrieves the internal container id for a given URI.

        Args:
            uri (str): The URI to look up.

        Returns:
            str: The associated internal container id.
        """
        result = self._execute('''
            SELECT id FROM local_instances WHERE ip = ?
        ''', (uri,))
        row = result.fetchone()
        if row:
            return row['id']
        
        log.LOGGER(f'Container not found for URI: {uri}')

    def get_balance_by_father_id(self, id: str) -> int:
        """
        Retrieves the balance_mu amount for a father ID, checking both clients and internal containers.

        Args:
            id (str): The father ID.

        Returns:
            int: The balance_mu amount.
        """
        if self.client_exists(client_id=id):
            return self.get_balance_by_client_id(id=id)
        elif self.internal_instance_exists(id=id):
            return self.get_instance_balance(id=id)
        # An unknown father has no balance. It used to report a flat default here, which
        # invented funds nobody had paid for.
        log.LOGGER(f'No client or instance found for father id {id}; reporting zero balance.')
        return 0

    # Payment system
    def add_deposit_token(self, client_id: str, status: str) -> str:
        """
        Adds a deposit token to the database.

        Args:
            client_id (str): The ID of the client associated with the deposit token.
            status (str): The status of the deposit token (pending, payed, or rejected).
        """
        if status not in ('pending', 'payed', 'rejected'):
            raise ValueError("Invalid status. Status must be one of: 'pending', 'payed', 'rejected'.")

        token_id = str(uuid.uuid4())
        self._execute('''
            INSERT INTO deposit_tokens (id, client_id, status)
            VALUES (?, ?, ?)
        ''', (token_id, client_id, status))

        return token_id

    def get_deposit_tokens(self, status: Optional[str] = None) -> List[dict]:
        """
        Fetches all deposit tokens from the database, optionally filtering by status.

        Args:
            status (Optional[str]): The status to filter deposit tokens by (pending, payed, rejected).

        Returns:
            List[dict]: A list of dictionaries containing deposit token details.
        """
        query = "SELECT id, client_id, status FROM deposit_tokens"
        params = []

        if status:
            if status not in ('pending', 'payed', 'rejected'):
                raise ValueError("Invalid status. Status must be one of: 'pending', 'payed', 'rejected'.")
            query += " WHERE status = ?"
            params.append(status)

        result = self._execute(query, tuple(params))
        tokens = [{'id': row['id'], 'client_id': row['client_id'], 'status': row['status']} for row in result.fetchall()]

        return tokens

    def client_id_from_deposit_token(self, token_id: str) -> str:
        """
        Retrieves the client ID associated with a given deposit token ID.

        Args:
            token_id (str): The ID of the deposit token.

        Returns:
            str: The client ID associated with the deposit token.

        Raises:
            ValueError: If the deposit token ID does not exist.
        """
        query = "SELECT client_id FROM deposit_tokens WHERE id = ?"
        result = self._execute(query, (token_id,))

        row = result.fetchone()
        if row is None:
            raise ValueError(f"Deposit token with ID '{token_id}' does not exist.")

        return row['client_id']

    def update_deposit_token(self, token_id: str, status: Optional[str] = None):
        """
        Updates the status of a deposit token in the database.

        Args:
            token_id (str): The ID of the deposit token to update.
            status (Optional[str]): The new status of the deposit token (if provided).
        """
        if status and status not in ('pending', 'payed', 'rejected'):
            raise ValueError("Invalid status. Status must be one of: 'pending', 'payed', 'rejected'.")

        updates = []
        params = []

        if status is not None:
            updates.append("status = ?")
            params.append(status)

        if not updates:
            raise ValueError("No values to update.")

        params.append(token_id)
        query = f"UPDATE deposit_tokens SET {', '.join(updates)} WHERE id = ?"
        self._execute(query, tuple(params))

    def deposit_token_exists(self, token_id: str, status: Optional[str] = None) -> bool:
        """
        Checks if a deposit token exists in the database, optionally filtering by status.

        Args:
            token_id (str): The ID of the deposit token to check.
            status (Optional[str]): The status to filter by (pending, payed, rejected). If None, the status is not considered.

        Returns:
            bool: True if the deposit token exists with the given status (if provided), False otherwise.
        """
        query = "SELECT 1 FROM deposit_tokens WHERE id = ?"
        params = [token_id]

        if status:
            if status not in ('pending', 'payed', 'rejected'):
                raise ValueError("Invalid status. Status must be one of: 'pending', 'payed', 'rejected'.")
            query += " AND status = ?"
            params.append(status)

        query += " LIMIT 1"
        result = self._execute(query, tuple(params))

        return result.fetchone() is not None

    def delete_deposit_token(self, token_id: str):
        """
        Deletes a deposit token from the database.

        Args:
            token_id (str): The ID of the deposit token to delete.
        """
        self._execute('''
            DELETE FROM deposit_tokens WHERE id = ?
        ''', (token_id,))

    def insert_energy_record(self, cpu_percent: float, memory_usage: float,
                           power_consumption: float, cost: float):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO energy_consumption
            (timestamp, cpu_percent, memory_usage, power_consumption, cost)
            VALUES (?, ?, ?, ?, ?)
        ''', (datetime.now(), cpu_percent, memory_usage, power_consumption, cost))
        self.conn.commit()

    def get_latest_energy_records(self, limit: int = 100) -> Generator[Dict, None, None]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM energy_consumption
            ORDER BY timestamp DESC LIMIT ?
        ''', (limit,))
        columns = [description[0] for description in cursor.description]
        for row in cursor.fetchall():
            yield dict(zip(columns, row))

def is_peer_available(peer_id: str, min_slots_open: int = 1) -> bool:
    # Slot concept here refers to the number of urls. Slot should be renamed on all the code because is incorrectly used.
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
        if min_slots_open == 1:
            # Short-circuit on the first reachable address. Materialising the whole
            # list would probe every address the peer ever announced, each up to the
            # 1s is_open timeout, just to answer "is at least one open?".
            return next(generate_uris_by_peer_id(peer_id), None) is not None
        return len(list(generate_uris_by_peer_id(peer_id))) >= min_slots_open
    except Exception:
        return False
