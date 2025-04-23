import sqlite3
from src.utils.env import EnvManager

env_manager = EnvManager()
DATABASE_FILE = env_manager.get_env("DATABASE_FILE")

def list_instances():
    """
    Lists all service instances (internal and external) stored in the database.
    Each entry includes:
      - ID
      - IP (if available)
      - Parent ID
      - Parent type ('internal_service' or 'client')
      - Computed gas value (for internal; else 'N/A')
      - Location: 'local' for internal or peer ID for external
    If a table does not exist, prints a warning.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    instances = []
    try:
        # Load parent ID sets
        cursor.execute("SELECT id FROM internal_services;")
        internal_ids = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT id FROM clients;")
        client_ids = {row[0] for row in cursor.fetchall()}

        # Fetch internal services
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='internal_services';")
        if cursor.fetchone():
            cursor.execute(
                "SELECT id, ip, father_id, gas_mantissa, gas_exponent FROM internal_services"
            )
            for id_, ip, father_id, gm, ge in cursor.fetchall():
                parent_type = (
                    'local instance' if father_id in internal_ids else
                    'client' if father_id in client_ids else
                    'unknown'
                )
                gas_str = f"{gm * (10 ** ge):.6e}"
                instances.append({
                    'id': id_,
                    'ip': ip or 'N/A',
                    'parent_id': father_id or 'None',
                    'parent_type': parent_type,
                    'gas': gas_str,
                    'location': 'local'
                })
        else:
            print("Warning: 'internal_services' table missing.")

        # Fetch external services
        if False:  # TODO Check if there are needed more columns on external_services table.
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='external_services';")
            if cursor.fetchone():
                cursor.execute(
                    "SELECT token, peer_id, client_id FROM external_services"
                )
                for token, peer_id, client_id in cursor.fetchall():
                    parent_type = (
                        'client' if client_id in client_ids else
                        'unknown'
                    )
                    instances.append({
                        'id': token,
                        'ip': 'N/A',
                        'parent_id': client_id or 'None',
                        'parent_type': parent_type,
                        'gas': 'N/A',
                        'location': peer_id or 'N/A'
                    })
            else:
                print("Warning: 'external_services' table missing.")

        # Unified listing
        print("Service Instances:\n")
        if not instances:
            print("No service instances found.")
            return

        for inst in instances:
            print(f"""
ID: {inst['id']}
IP: {inst['ip']}
Parent ID: {inst['parent_id']}
Parent Type: {inst['parent_type']}
Gas: {inst['gas']}
Location: {inst['location']}
"""
            )

    except sqlite3.Error as e:
        print(f"An error occurred while listing instances: {e}")
    finally:
        conn.close()
