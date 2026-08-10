import sqlite3
from src.utils.config import ConfigManager
from src.utils.logger import ssformat
from src.utils.monetary import mu_to_erg_str

env_manager = ConfigManager()
DATABASE_FILE = env_manager.get("DATABASE_FILE")


def list_clients():
    """
    Lists all clients stored in the database, showing grouped information in sections:
      1. General
      2. Balance & Usage
    If the table does not exist, it prints a warning message.
    """
    # Connect to the SQLite database
    connection = sqlite3.connect(DATABASE_FILE)
    cursor = connection.cursor()

    try:
        # Check if the 'clients' table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='clients';"
        )
        if not cursor.fetchone():
            print("Warning: The 'clients' table does not exist in the database.")
            return

        # Query the clients table for all columns
        cursor.execute(
            '''
            SELECT id, balance_mu, last_usage
            FROM clients
            '''
        )
        clients = cursor.fetchall()

        if not clients:
            print("No clients found.")
            return

        for client in clients:
            client_id, balance_mu, last_usage = client

            # Section: General
            print(f"ID: {client_id}")

            # Section: Balance & Usage
            print("[Balance & Usage]")
            print(f"  Balance: {mu_to_erg_str(int(balance_mu))} ERG")
            print(f"  Last Usage: {last_usage if last_usage is not None else 'None'}")
            print()

            print("-" * 40 + "\n")

    except sqlite3.Error as e:
        print(f"An error occurred while listing clients: {e}")
    finally:
        # Close the database connection
        connection.close()
