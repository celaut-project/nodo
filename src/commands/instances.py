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
      - Computed gas value (for internal; else 'N/A')
      - Serialized instance data
      - Location tag ('local' for internal or peer ID for external)
    If a table does not exist, prints a warning.
    """
    connection = sqlite3.connect(DATABASE_FILE)
    cursor = connection.cursor()

    instances = []
    try:
        # Internal services
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='internal_services';"
        )
        if cursor.fetchone():
            cursor.execute(
                '''
                SELECT id, ip, father_id, gas_mantissa, gas_exponent, serialized_instance
                FROM internal_services
                '''
            )
            for id_, ip, father_id, gas_mantissa, gas_exponent, serialized in cursor.fetchall():
                gas_value = gas_mantissa * (10 ** gas_exponent)
                gas_str = f"{gas_value:.6e}"
                instances.append({
                    'id': id_,
                    'ip': ip,
                    'parent_id': father_id,
                    'gas': gas_str,
                    'serialized': serialized,
                    'location': 'local'
                })
        else:
            print("Warning: The 'internal_services' table does not exist in the database.")

        # External services
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='external_services';"
        )
        if cursor.fetchone():
            cursor.execute(
                '''
                SELECT token, NULL as ip, NULL as father_id, NULL as gas_mantissa, NULL as gas_exponent, serialized_instance, peer_id
                FROM external_services
                '''
            )
            for token, ip, father_id, gm, ge, serialized, peer_id in cursor.fetchall():
                instances.append({
                    'id': token,
                    'ip': ip,
                    'parent_id': father_id,
                    'gas': 'N/A',
                    'serialized': serialized,
                    'location': peer_id
                })
        else:
            print("Warning: The 'external_services' table does not exist in the database.")

        print("Service Instances:\n")
        if instances:
            for inst in instances:
                print(f"""
ID: {inst['id']}
IP: {inst['ip'] or 'N/A'}
Parent ID: {inst['parent_id'] if inst['parent_id'] else 'None'}
Gas: {inst['gas']}
Serialized Instance: {inst['serialized']}
Location: {inst['location']}
                """
                )
        else:
            print("No service instances found.")

    except sqlite3.Error as e:
        print(f"An error occurred while listing instances: {e}")
    finally:
        connection.close()
