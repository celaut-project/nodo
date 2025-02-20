import sqlite3
from src.utils.env import EnvManager

env_manager = EnvManager()

DATABASE_FILE = env_manager.get_env("DATABASE_FILE")

def list_peers():
    """
    Lists all peers stored in the database, showing all available information.
    If the table does not exist, it prints a warning message.
    """
    # Connect to the SQLite database
    connection = sqlite3.connect(DATABASE_FILE)
    cursor = connection.cursor()

    try:
        # Check if the 'peer' table exists
        cursor.execute('''
            SELECT name FROM sqlite_master WHERE type='table' AND name='peer';
        ''')
        table_exists = cursor.fetchone()

        if not table_exists:
            print("Warning: The 'peer' table does not exist in the database.")
            return

        # Query the peer table for all columns
        cursor.execute('''
            SELECT 
                id, protocol_stack, client_id, 
                gas_mantissa, gas_exponent, gas_last_update, 
                reputation_proof_id, reputation_score, 
                reputation_index, last_index_on_ledger 
            FROM peer
        ''')
        peers = cursor.fetchall()

        print("Peers:\n")
        if peers:
            for peer in peers:
                (
                    peer_id, protocol_stack, client_id,
                    gas_mantissa, gas_exponent, gas_last_update,
                    reputation_proof_id, reputation_score,
                    reputation_index, last_index_on_ledger
                ) = peer

                print(f"""
ID: {peer_id}
Protocol stack: {protocol_stack if protocol_stack else 'None'}
Client ID: {client_id}
Gas Mantissa: {gas_mantissa}
Gas Exponent: {gas_exponent}
Gas Last Update: {gas_last_update if gas_last_update else 'None'}
Reputation Proof ID: {reputation_proof_id if reputation_proof_id else 'None'}
Reputation Score: {reputation_score}
Reputation Index: {reputation_index}
Last Index on Ledger: {last_index_on_ledger}
                """)
        else:
            print("No peers found.")
    except sqlite3.Error as e:
        print(f"An error occurred while listing peers: {e}")
    finally:
        # Close the database connection
        connection.close()
