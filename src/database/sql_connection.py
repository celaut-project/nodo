import datetime
import os
import math
import uuid
import sqlite3
import time
from hashlib import sha3_256
from threading import Lock
from typing import Callable, Dict, Generator, List, Tuple, Optional
from google.protobuf.json_format import MessageToJson

import grpc
from bee_rpc import client as bee

from protos import gateway_pb2_grpc, gateway_pb2, celaut_pb2
from src.utils import logger as log, logger
from src.utils.env import (
    SHA3_256_ID,
    DOCKER_CLIENT,
    EnvManager
)
from src.utils.singleton import Singleton
from src.utils.utils import from_gas_amount, generate_uris_by_peer_id

env_manager = EnvManager()

CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME = env_manager.get_env("CLIENT_MIN_GAS_AMOUNT_TO_RESET_EXPIRATION_TIME")
TOTAL_REPUTATION_TOKEN_AMOUNT = int(env_manager.get_env("TOTAL_REPUTATION_TOKEN_AMOUNT"))
CLIENT_EXPIRATION_TIME = env_manager.get_env("CLIENT_EXPIRATION_TIME")
STORAGE = env_manager.get_env("STORAGE")
DATABASE_FILE = env_manager.get_env("DATABASE_FILE")
DEFAULT_INTIAL_GAS_AMOUNT = env_manager.get_env("DEFAULT_INTIAL_GAS_AMOUNT")

# Define a maximum mantissa and exponent
MAX_MANTISSA = 10**3  # Adjust this limit as needed
MAX_EXPONENT = 1024  # Adjust this limit as needed


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

    def get_client_gas(self, client_id: str) -> Optional[Tuple[int, float, str]]:
        """
        Retrieves the gas and last usage time for a client.

        Args:
            client_id (str): The ID of the client.

        Returns:
            Tuple[int, float, str]: The gas amount, last usage time and gas in scientific notation.
        """
        result = self._execute('''
            SELECT gas_mantissa, gas_exponent, last_usage FROM clients WHERE id = ?
        ''', (client_id,))
        row = result.fetchone()
        if row:
            return (
                _combine_gas(mantissa=row['gas_mantissa'], exponent=row['gas_exponent']),
                row['last_usage'],
                f"{row['gas_mantissa']}e{row['gas_exponent']}"
            )
                
        log.LOGGER(f'Client not found: {client_id}')
        return None

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
        _gas, _last_usage, _ = self.get_client_gas(client_id)
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
        _gas, _last_usage, _ = self.get_client_gas(client_id)
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
        _gas, _last_usage, _ = self.get_client_gas(client_id)
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

    def add_internal_service(self, father_id: str, container_ip: str, container_id: str, gas: int, serialized_instance: str,):
        """
        Adds an internal service to the database.

        Args:
            father_id (str): The father ID.
            container_ip (str): The IP address of the container.
            container_id (str): The container ID.
            gas (int): The gas amount.
            serialized_instance (str): Serialized celaut instance
        """
        gas_mantissa, gas_exponent = _split_gas(gas)
        _validate_gas(gas_mantissa, gas_exponent)
        self._execute('''
            INSERT INTO internal_services (id, ip, father_id, gas_mantissa, gas_exponent, mem_limit, serialized_instance)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (container_id, container_ip, father_id, gas_mantissa, gas_exponent, 0, serialized_instance))
        log.LOGGER(f'Saved service {container_id} as dependency of {father_id}')

    def update_sys_req(self, id: str, mem_limit: Optional[int]) -> bool:
        """
        Updates system requirements for an internal service.

        Args:
            id (str): The id of the internal service.
            mem_limit (Optional[int]): The new memory limit.

        Returns:
            bool: True if update was successful, False otherwise.
        """
        try:
            self._execute('''
                UPDATE internal_services SET mem_limit = ? WHERE id = ?
            ''', (mem_limit, id))
            return True
        except:
            return False

    def get_sys_req(self, id: str) -> dict:
        """
        Retrieves system requirements for an internal service.

        Args:
            id (str): The id of the internal service.

        Returns:
            dict: A dictionary containing the system requirements.
        """
        result = self._execute('''
            SELECT mem_limit FROM internal_services WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        if row:
            return row
        raise Exception(f'Internal service {id}')

    def get_internal_service_gas(self, id: str) -> int:
        """
        Retrieves the gas amount for an internal service.

        Args:
            id (str): The id of the internal service.

        Returns:
            int: The gas amount.
        """
        result = self._execute('''
            SELECT gas_mantissa, gas_exponent FROM internal_services WHERE id = ?
        ''', (id,))
        row = result.fetchone()
        if row:
            return row['gas_mantissa'] * (10 ** row['gas_exponent'])
        raise Exception(f'Internal service {id}')

    def get_all_internal_service_ids(self) -> List[str]:
        """
        Fetches all ids of internal services.

        Returns:
            List[str]: A list of ids.
        """
        result = self._execute('''
            SELECT father_id, ip, id FROM internal_services
        ''')
        return [row['id'] for row in result.fetchall()]

    def update_gas_to_container(self, id: str, gas: int):
        """
        Updates the gas amount for a container.

        Args:
            id (str): The id of the container.
            gas (int): The new gas amount.
        """
        gas_mantissa, gas_exponent = _split_gas(gas)
        _validate_gas(gas_mantissa, gas_exponent)
        self._execute('''
            UPDATE internal_services SET gas_mantissa = ?, gas_exponent = ? WHERE id = ?
        ''', (gas_mantissa, gas_exponent, id))

    def container_exists(self, id: str) -> bool:
        """
        Checks if a container exists in the database.

        Args:
            id (str): The id of the container.

        Returns:
            bool: True if the container exists, False otherwise.
        """
        result = self._execute('''
            SELECT COUNT(*)
            FROM internal_services
            WHERE id = ?
        ''', (id,))
        return result.fetchone()[0] > 0

    def purge_internal(self, id: str):
        """
        Purges an internal service

        Args:
            id (str): The id of the internal service.

        """
        self._execute('''
            DELETE FROM internal_services WHERE id = ?
        ''', (id,))

    def get_internal_father_id(self, id: str) -> str:
        """
        Retrieves the father_id of an internal service.

        Args:
            id (str): The id of the internal service.

        Returns:
            str: The father_id of the internal service, or an empty string if not found.
        """
        cursor = self._execute('''
            SELECT father_id
            FROM internal_services
            WHERE id = ?
        ''', (id,))
        result = cursor.fetchone()
        return result[0] if result else ""

    def get_internal_instance(self, id: str) -> Optional[str]:
        """
        Retrieves the serialized_instance of an internal service.

        Args:
            id (str): The id of the internal service.

        Returns:
            str: The serialized_instance of the internal service, or None if not found.
        """
        cursor = self._execute('''
            SELECT serialized_instance
            FROM internal_services
            WHERE id = ?
        ''', (id,))
        result = cursor.fetchone()
        return result[0] if result else None

    def get_internal_ip(self, id: str) -> Optional[str]:
        """
        Retrieves the IP address of an internal service.

        Args:
            id (str): The id of the internal service.

        Returns:
            Optional[str]: The IP address of the internal service, or None if not found.
        """
        cursor = self._execute('''
            SELECT ip
            FROM internal_services
            WHERE id = ?
        ''', (id,))
        result = cursor.fetchone()
        return result[0] if result else None

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
            # Fetch all peers' data along with slots, URIs, and contracts in one query
            result = self._execute('''
                SELECT
                    p.id,
                    p.reputation_proof_id,
                    p.reputation_score,
                    p.reputation_index,
                    p.last_index_on_ledger,
                    p.app_protocol,
                    s.internal_port,
                    u.ip,
                    u.port
                FROM peer p
                -- Joining slot table to get information about ports
                LEFT JOIN slot s ON s.peer_id = p.id
                -- Joining uri table to get IP and port details for each slot
                LEFT JOIN uri u ON u.slot_id = s.id
            ''')

            rows = result.fetchall()

            if not rows and not force_submit:
                return True

            # Fetch the total sum of all reputation amounts from the table
            total_amount_result = self._execute('SELECT SUM(reputation_score) AS total_amount FROM peer')
            total_amount_row = total_amount_result.fetchone()
            total_amount = total_amount_row['total_amount'] or 0

            # Dictionary to store instance data (for peers with multiple slots or contracts)
            peers_dict = {}
            for row in rows:
                peer_id = row['id']
                if peer_id not in peers_dict:
                    # Initialize the instance for this peer
                    instance = celaut_pb2.Instance()

                    # Set app_protocol if available
                    if row['app_protocol']:
                        instance.api.app_protocol.ParseFromString(row['app_protocol'])

                    # Store in the dict
                    peers_dict[peer_id] = {
                        'instance': instance,
                        'reputation_proof_id': row['reputation_proof_id'],
                        'reputation_score': row['reputation_score'] or 0,
                        'reputation_index': row['reputation_index'] or 0,
                        'last_index_on_ledger': row['last_index_on_ledger'] or 0
                    }

                # Add slots and URIs to the instance
                if row['internal_port']:
                    slot = peers_dict[peer_id]['instance'].uri_slot.add()
                    slot.internal_port = row['internal_port']
                    if row['ip'] and row['port']:
                        uri = slot.uri.add()
                        uri.ip = row['ip']
                        uri.port = row['port']

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
                        # Convert instance to JSON string
                        instance_json = MessageToJson(data['instance'])

                        # Calculate the percentage of the total reputation token amount
                        if reputation_index - last_index_on_ledger >= env_manager.get_env("LEDGER_REPUTATION_SUBMISSION_THRESHOLD"):
                            logger.LOGGER(f'Peer {peer_id} with proof {reputation_proof_id} meets the submission threshold.')
                            needs_submit = True
                            percentage_amount = (reputation_score / total_amount) * token_amount if total_amount else 0
                            to_submit.append((reputation_proof_id, percentage_amount, instance_json))

                        # Proof percentage doesn't need to be changed itself, but needs to be updated if others do.
                        elif last_index_on_ledger > 0:
                            logger.LOGGER(f'Peer {peer_id} with proof {reputation_proof_id} does not meet the submission threshold, but is included in the proof.')
                            percentage_amount = (reputation_score / total_amount) * token_amount if total_amount else 0
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

    def update_double_attempt_retry_time_on_ledger(self, ledger: str):
        """
        Updates the double_spending_retry_time field in the ledger table
        by setting it to the current time plus 10 minutes for the specified ledger.

        Args:
            ledger (str): The identifier of the ledger whose retry_time needs to be updated.
        """
        query = """
        UPDATE ledger
        SET double_spending_retry_time = DATETIME('now', '+10 minutes')
        WHERE id = ?
        """

        # Execute the query, passing the ledger ID to update the appropriate record.
        self._execute(query, (ledger,))

    def check_if_ledger_is_available(self, ledger: str) -> bool:
        """
        Checks if the specified ledger is available for use.
        A ledger is considered available if its double_spending_retry_time is NULL
        or is in the past.

        Args:
            ledger (str): The identifier of the ledger to check.

        Returns:
            bool: True if the ledger is available, False otherwise.
        """
        query = """
        SELECT double_spending_retry_time
        FROM ledger
        WHERE id = ?
        """

        # Execute the query to get the retry time for the specified ledger.
        result = self._execute(query, (ledger,)).fetchone()

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
                # Combine gas values into a single 'gas' key
                peer_info['gas'] = _combine_gas(peer_info.pop('gas_mantissa'), peer_info.pop('gas_exponent'))
                return peer_info
            else:
                return {}  # Return empty dict if peer not found
        except Exception as e:
            logger.LOGGER(f'Error fetching peer details for ID {peer_id}: {e}')
            return {}

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

    def add_gas_to_peer(self, peer_id: str, gas: int) -> bool:
        """
        Adds the specified amount of gas to the existing gas value of a peer.

        Parameters:
        - peer_id (str): The unique identifier of the peer.
        - gas (int): The amount of gas to be added to the peer's existing gas.

        Returns:
        - bool: True if the operation was successful, False otherwise.
        """
        try:
            # Retrieve the current gas values from the database.
            result = self._execute('SELECT gas_mantissa, gas_exponent FROM peer WHERE id = ?', (peer_id,))
            row = result.fetchone()

            if row:
                # Combine mantissa and exponent to get the current gas amount.
                current_gas = _combine_gas(row['gas_mantissa'], row['gas_exponent'])

                # Add the specified gas to the current amount.
                total_gas = current_gas + gas

                # Split the new total gas into mantissa and exponent.
                new_mantissa, new_exponent = _split_gas(total_gas)

                # Validate the new mantissa and exponent values.
                _validate_gas(new_mantissa, new_exponent)

                # Get the current timestamp for gas_last_update.
                current_time = datetime.datetime.now().isoformat()

                # Update the peer's gas values and gas_last_update in the database.
                self._execute('''
                    UPDATE peer SET gas_mantissa = ?, gas_exponent = ?, gas_last_update = ? WHERE id = ?
                ''', (new_mantissa, new_exponent, current_time, peer_id))

                return True
            else:
                raise Exception(f'Peer not found: {peer_id}')
        except Exception as e:
            logger.LOGGER(f'Error adding gas to peer {peer_id}: {e}')
            return False

    def refresh_gas_for_peer(self, peer_id: str, gas: int) -> bool:
        """
        Sets the gas value of a peer to a specified amount, replacing any existing value.

        Parameters:
        - peer_id (str): The unique identifier of the peer.
        - gas (int): The new gas amount to set for the peer.

        Returns:
        - bool: True if the operation was successful, False otherwise.
        """
        try:
            # Split the provided gas value into mantissa and exponent.
            new_mantissa, new_exponent = _split_gas(gas)

            # Validate the new gas values.
            _validate_gas(new_mantissa, new_exponent)

            # Get the current timestamp for gas_last_update.
            current_time = datetime.datetime.now().isoformat()

            # Update the peer's gas and gas_last_update directly in the database.
            self._execute('''
                UPDATE peer SET gas_mantissa = ?, gas_exponent = ?, gas_last_update = ? WHERE id = ?
            ''', (new_mantissa, new_exponent, current_time, peer_id))

            return True
        except Exception as e:
            logger.LOGGER(f'Error refreshing gas for peer {peer_id}: {e}')
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

    def add_contract(self, contract: celaut_pb2.ContractLedger, peer_id: str = "LOCAL"):
        """
        Adds a contract to the database.

        Args:
            contract (celaut_pb2.ContractLedger): The contract to add.
            peer_id (Optional[str]): The ID of the peer or None for a self contract (to be send to clients.)
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

        self._execute("INSERT OR IGNORE INTO contract_instance (address, ledger_id, contract_hash, peer_id) "
                    "VALUES (?,?,?,?)", (address, ledger, contract_hash, peer_id))

    def add_reputation_proof(self, contract_ledger: celaut_pb2.ContractLedger, peer_id: str) -> bool:
        """
        Add or update the reputation_proof_id for a peer.

        Args:
            peer_id (str): The ID of the peer whose reputation_proof_id is to be updated.
            contract_ledger (celaut_pb2.ContractLedger): The reputation proof contract ledger.

        Returns:
            bool: True if the update was successful, False otherwise.
        """

        try:
            new_proof_id = contract_ledger.contract_addr

            # Fetch the peer to ensure it exists
            result = self._execute('SELECT id FROM peer WHERE id = ?', (peer_id,))
            row = result.fetchone()

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

    def instance_exists(self, instance: gateway_pb2.Instance) -> bool:
        """
        Checks if any URI within an instance exists in the database.

        Args:
            instance (gateway_pb2.Instance): The instance containing URI slots to be checked.

        Returns:
            bool: 
                - True if at least one URI from the instance exists in the database.
                - True in case of an unexpected error (failsafe behavior).
                - False if no URIs exist in the database or the instance is empty.
        """
        try:
            # Iterate through URI slots and URIs to check existence
            for slot in instance.instance.uri_slot:
                for uri in slot.uri:
                    if self.uri_exists(uri=uri):
                        return True
        except Exception as e:
            logger.LOGGER(f"Error while checking instance existence: {e}")
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
                UPDATE peer SET client_id = ? WHERE id = ?
            ''', (client_id, peer_id))
            logger.LOGGER(f'Associated external client {client_id} with peer {peer_id}')
            return True
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to associate external client {client_id} with peer {peer_id}: {e}')
            return False

    def get_external_father_id(self, token: str) -> str:
        """
        Retrieves the father_id of an external service based on the token.

        Args:
            token (str): The token of the external service.

        Returns:
            str: The father_id of the external service, or an empty string if not found.
        """
        cursor = self._execute('''
            SELECT client_id
            FROM external_services
            WHERE token = ?
        ''', (token,))
        result = cursor.fetchone()
        return result[0] if result else ""

    def get_external_instance(self, token: str) -> Optional[str]:
        """
        Retrieves the serialized_service of an external service based on the token.

        Args:
            token (str): The token of the external service.

        Returns:
            str: The serialized_service of the external service, or None if not found.
        """
        cursor = self._execute('''
            SELECT serialized_service
            FROM external_services
            WHERE token = ?
        ''', (token,))
        result = cursor.fetchone()
        return result[0] if result else None

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

    def add_external_service(self, client_id: str, encrypted_external_token: str, external_token: str, peer_id: str, serialized_instance: str):
        """
        Adds an external service to the database.

        Args:
            client_id (str): The client ID.
            encrypted_external_token (str): The encrypted external token.
            external_token (str): The external token.
            peer_id (str): The peer ID.
            serialized_instance (str): Serialized celaut instance
        """
        self._execute('''
            INSERT INTO external_services (token, token_hash, peer_id, client_id, serialized_instance)
            VALUES (?, ?, ?, ?, ?)
        ''', (external_token, encrypted_external_token, peer_id, client_id, serialized_instance))

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
            logger.LOGGER(f'Failed to retrieve token for hashed external service token {hashed_token}: {e}')
            return None

    def get_peer_id_by_external_service(self, token: str) -> Optional[str]:
        """
        Retrieves the peer id where the external service was requested.

        Args:
            token (str): The token of the external service.

        Returns:
            Optional[str]: The peer id if it exists, or None if not found.
        """
        try:
            result = self._execute('''
                SELECT peer_id FROM external_services WHERE token = ?
            ''', (token,))
            row = result.fetchone()
            if row:
                return row['peer_id']
            return None
        except sqlite3.Error as e:
            logger.LOGGER(f'Failed to retrieve peer_id for external service {token}: {e}')
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
            refund = from_gas_amount(next(bee.client_grpc(
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
            log.LOGGER('Error during remove a container on ' + peer_id + ' ' + str(e))

        return refund

    # Common Methods

    def get_internal_service_id_by_uri(self, uri: str) -> Optional[str]:
        """
        Retrieves the internal service id for a given URI.

        Args:
            uri (str): The URI to look up.

        Returns:
            str: The associated internal service id.
        """
        result = self._execute('''
            SELECT id FROM internal_services WHERE ip = ?
        ''', (uri,))
        row = result.fetchone()
        if row:
            return row['id']
        
        log.LOGGER(f'Internal service not found for URI: {uri}')

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
        elif self.container_exists(id=id):
            return self.get_internal_service_gas(id=id)
        else:
            return int(DEFAULT_INTIAL_GAS_AMOUNT)

    # Tunnel system

    def add_tunnel(self, uri: str, service: str, live: bool):
        """
        Adds a tunnel to the database.

        Args:
            tunnel_id (str): The ID of the tunnel.
            uri (str): The URI of the tunnel.
            service (str): The service associated with the tunnel.
            live (bool): Whether the tunnel is live or not.
        """
        tunnel_id = str(uuid.uuid4())
        self._execute('''
            INSERT INTO tunnels (id, uri, service, live)
            VALUES (?, ?, ?, ?)
        ''', (tunnel_id, uri, service, live))

    def get_tunnels(self) -> List[dict]:
        """
        Fetches all tunnels from the database.

        Returns:
            List[dict]: A list of dictionaries containing tunnel details.
        """
        result = self._execute("SELECT id, uri, service, live FROM tunnels")
        tunnels = [{'id': row['id'], 'uri': row['uri'], 'service': row['service'], 'live': row['live']} for row in result.fetchall()]
        logger.LOGGER(f'Found tunnels: {tunnels}')
        return tunnels

    def update_tunnel(self, tunnel_id: str, uri: Optional[str] = None, service: Optional[str] = None, live: Optional[bool] = None):
        """
        Updates a tunnel in the database.

        Args:
            tunnel_id (str): The ID of the tunnel to update.
            uri (Optional[str]): The new URI of the tunnel (if provided).
            service (Optional[str]): The new service of the tunnel (if provided).
            live (Optional[bool]): The new live status of the tunnel (if provided).
        """
        updates = []
        params = []

        if uri is not None:
            updates.append("uri = ?")
            params.append(uri)

        if service is not None:
            updates.append("service = ?")
            params.append(service)

        if live is not None:
            updates.append("live = ?")
            params.append(live)

        if not updates:
            raise ValueError("No values to update.")

        params.append(tunnel_id)
        query = f"UPDATE tunnels SET {', '.join(updates)} WHERE id = ?"
        self._execute(query, tuple(params))

    def delete_tunnel(self, tunnel_id: str):
        """
        Deletes a tunnel from the database.

        Args:
            tunnel_id (str): The ID of the tunnel to delete.
        """
        self._execute('''
            DELETE FROM tunnels WHERE id = ?
        ''', (tunnel_id,))

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
        return any(list(generate_uris_by_peer_id(peer_id))) if min_slots_open == 1 else \
            len(list(generate_uris_by_peer_id(peer_id))) >= min_slots_open
    except Exception:
        return False
