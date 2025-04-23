import sqlite3
from src.utils.env import EnvManager

env_manager = EnvManager()
DATABASE_FILE = env_manager.get_env("DATABASE_FILE")

def list_instances(groupable: bool = False):
    """
    Lists all service instances (internal and external) stored in the database.
    Each entry includes:
      - ID
      - IP
      - Parent ID
      - Parent type ('internal_service' or 'client' or 'unknown')
      - Gas (computed or 'N/A')
      - Location: 'local' for internal or peer ID for external
    If groupable=True, displays them in a parent-children tree.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    instances = []
    try:
        # Load parent ID sets for resolution
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
                    'internal_service' if father_id in internal_ids else
                    'client' if father_id in client_ids else
                    'unknown'
                )
                gas_str = f"{gm * (10 ** ge):.6e}"
                instances.append({
                    'id': id_,
                    'ip': ip or 'N/A',
                    'parent_id': father_id,
                    'parent_type': parent_type,
                    'gas': gas_str,
                    'location': 'local'
                })

        # Fetch external services  
        # TODO check if it's needed to add more columns on external_services DB.   What is exactly the client_id column?  Why is named client and not parent?
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='external_services';")
        if cursor.fetchone():
            cursor.execute(
                "SELECT token, peer_id, client_id FROM external_services"
            )
            for token, peer_id, client_id in cursor.fetchall():
                parent_type = 'client' if client_id in client_ids else 'unknown'
                instances.append({
                    'id': token,
                    'ip': 'N/A',
                    'parent_id': client_id,
                    'parent_type': parent_type,
                    'gas': 'N/A',
                    'location': peer_id or 'N/A'
                })

    except sqlite3.Error as e:
        print(f"An error occurred while listing instances: {e}")
        return
    finally:
        conn.close()

    if not instances:
        print("No service instances found.")
        return

    # Helper: build tree if grouping requested
    if groupable:
        # Map id to instance and build children map
        inst_map = {inst['id']: inst for inst in instances}
        children = {inst['id']: [] for inst in instances}
        roots = []
        for inst in instances:
            pid = inst['parent_id']
            if pid and pid in children:
                children[pid].append(inst['id'])
            else:
                roots.append(inst['id'])

        def print_tree(node_id, prefix=""):
            inst = inst_map[node_id]
            print(f"{prefix}{inst['id']}")
            for child_id in children[node_id]:
                print_tree(child_id, prefix + "|   ")

        # Print each root tree
        for root_id in roots:
            print_tree(root_id)
    else:
        # Flat listing
        print("Service Instances:\n")
        for inst in instances:
            print(f"""
ID: {inst['id']}
IP: {inst['ip']}
Parent ID: {inst['parent_id'] or 'None'}
Parent Type: {inst['parent_type']}
Gas: {inst['gas']}
Location: {inst['location']}
"""
            )
