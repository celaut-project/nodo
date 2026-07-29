import sqlite3
from hashlib import sha3_256

from src.utils.config import ConfigManager
from protos import celaut_pb2 as celaut
from src.utils.logger import ssformat
from src.database.sql_connection import SQLConnection

env_manager = ConfigManager()
DATABASE_FILE = env_manager.get("DATABASE_FILE")
ERGO_LEDGER = "ergo"
ERGO_CONTRACT_HASH = sha3_256("proveDlog(decodePoint())".encode("utf-8")).hexdigest()

sq = SQLConnection()

def list_peers():
    """
    Lists all peers stored in the database, showing grouped information in sections:
      1. General
      2. Client & Gas
      3. Reputation
    If the table does not exist, it prints a warning message.
    """
    # Connect to the SQLite database
    connection = sqlite3.connect(DATABASE_FILE)
    cursor = connection.cursor()

    try:
        # Check if the 'peer' table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='peer';"
        )
        if not cursor.fetchone():
            print("Warning: The 'peer' table does not exist in the database.")
            return

        # Query the peer table for all columns
        cursor.execute(
            '''
            SELECT
                id, protocol_stack, remote_client_id,
                gas, gas_last_update,
                reputation_proof_id, reputation_score,
                reputation_index, last_index_on_ledger
            FROM peer
            '''
        )
        peers = cursor.fetchall()

        if not peers:
            print("No peers found.")
            return

        for peer in peers:
            (
                peer_id, protocol_stack, remote_client_id,
                gas_str, gas_last_update,
                reputation_proof_id, reputation_score,
                reputation_index, last_index_on_ledger
            ) = peer

            if protocol_stack:
                slot = celaut.Service.Api.Slot()
                slot.ParseFromString(protocol_stack)
                protocol_stack_tags = " ".join([p.tags[0] for p in slot.protocol_stack if p.tags])
            else:
                protocol_stack_tags = "N/A"

            gas = int(gas_str)
            gas_price = sq.get_peer_gas_price(peer_id=peer_id, contract_hash=ERGO_CONTRACT_HASH, ledger_hash=ERGO_LEDGER)
            gas_on_ergs = (gas/gas_price) if gas_price else 0

            # Section: General
            print(f"ID: {peer_id}")
            print("[General]")
            print(f"  Protocol stack: {protocol_stack_tags}")
            print()

            # Section: Client & Gas
            print("[Client & Gas]")
            print(f"  Remote Client ID: {remote_client_id}")
            print(f"  Gas/ERG: {ssformat(gas_price)}")
            print(f"  Gas: {ssformat(gas)}")
            print(f"       {ssformat(gas_on_ergs)} nanoERG")
            print(f"  Gas Last Update: {gas_last_update or 'None'}")
            print()

            # Section: Reputation
            print("[Reputation]")
            print(f"  Proof ID: {reputation_proof_id or 'None'}")
            print(f"  Score: {reputation_score or 'None'}")
            print(f"  Index: {reputation_index or 'None'}")
            print(f"  Last Index on Ledger: {last_index_on_ledger or 'None'}")
            print("-" * 40 + "\n")

    except sqlite3.Error as e:
        print(f"An error occurred while listing peers: {e}")
    finally:
        # Close the database connection
        connection.close()
