import sqlite3

from src.utils.config import ConfigManager
from protos import celaut_pb2 as celaut
from src.utils.logger import ssformat
from src.database.sql_connection import SQLConnection

env_manager = ConfigManager()
DATABASE_FILE = env_manager.get("DATABASE_FILE")

sq = SQLConnection()

def list_peers():
    """
    Lists all peers stored in the database, showing grouped information in sections:
      1. General
      2. Client & Gas
      3. Rates advertised by the peer
      4. Reputation
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
                id, advertisement, remote_client_id,
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
                peer_id, advertisement, remote_client_id,
                gas_str, gas_last_update,
                reputation_proof_id, reputation_score,
                reputation_index, last_index_on_ledger
            ) = peer

            advertised_rates = {}
            protocol_stack_tags = "N/A"
            if advertisement:
                announced = celaut.Peer()
                try:
                    announced.ParseFromString(advertisement)
                except Exception as e:
                    print(f"  (unreadable advertisement for {peer_id}: {e})")
                    announced = celaut.Peer()
                # The stack is per-address now, so show the union across the peer's
                # addresses rather than a single gateway slot's.
                tags = {
                    protocol.tags[0]
                    for uri in announced.uri
                    for protocol in uri.protocol_stack
                    if protocol.tags
                }
                if tags:
                    protocol_stack_tags = " ".join(sorted(tags))
                # Rates the peer advertised. Absent for peers running a version from
                # before nodes published them.
                advertised_rates = {
                    rate: gas.n for rate, gas in announced.gas_amount_per_call.items()
                }

            gas = int(gas_str)
            contracts = sq.get_peer_payment_contracts(peer_id)

            # Section: General
            print(f"ID: {peer_id}")
            print("[General]")
            print(f"  Protocol stack: {protocol_stack_tags}")
            print()

            # Section: Client & Gas
            print("[Client & Gas]")
            print(f"  Remote Client ID: {remote_client_id}")
            print(f"  Gas: {ssformat(gas)}")
            print(f"  Gas Last Update: {gas_last_update or 'None'}")
            print()

            # Section: Payment contracts
            # Every payment contract instance this peer has registered, across
            # every ledger and contract type -- not just a single hardcoded one.
            print("[Payment Contracts]")
            if contracts:
                for contract in contracts:
                    print(f"  Ledger: {contract['ledger_tag']}")
                    print(f"    Contract hash: {contract['contract_hash']}")
                    print(f"    Address:       {contract['address'] or 'N/A'}")
                    gas_price = contract['gas_price']
                    print(f"    Gas price:     {ssformat(gas_price) if gas_price is not None else 'N/A'}")
            else:
                print("  No payment contract registered for this peer.")
            print()

            # Section: Advertised rates
            # What this peer charges on a recurring basis, as it advertised. These
            # are ceilings, not quotes -- the price of a specific service still
            # comes from GetServiceEstimatedCost.
            print("[Rates] (advertised ceilings, gas)")
            if advertised_rates:
                for rate, gas_value in sorted(advertised_rates.items()):
                    print(f"  {rate}: {gas_value}")
            else:
                print("  Not advertised by this peer.")
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
