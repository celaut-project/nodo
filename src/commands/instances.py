import sqlite3
from src.utils.env import EnvManager
from protos import celaut_pb2 as celaut

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
    If groupable=True, displays them in a parent-children tree with full details.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    instances = []
    try:
        # Load parent ID sets for resolution
        cursor.execute("SELECT id FROM local_instances;")
        internal_ids = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT id FROM clients;")
        client_ids = {row[0] for row in cursor.fetchall()}
        
        def get_http_ip(serialized_instance: str) -> str:
            instance = celaut.Instance()
            instance.ParseFromString(serialized_instance)
            
            s = ""
            for _exp in instance.uri_slot:
                for _uri in _exp.uri:
                    s += f"\n  • {_uri.ip}:{_uri.port}  (#{_exp.internal_port})"
                    
            return s or "N/A"
                    
        # Fetch internal services
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='local_instances';")
        if cursor.fetchone():
            cursor.execute(
                "SELECT id, father_id, gas_mantissa, gas_exponent, serialized_instance FROM local_instances"
            )
            for id_, father_id, gm, ge, si in cursor.fetchall():        
                parent_type = (
                    'internal_service' if father_id in internal_ids else
                    'client' if father_id in client_ids else
                    'unknown'
                )
                gas_str = f"{gm * (10 ** ge):.6e}"
                instances.append({
                    'id': id_,
                    'ip': get_http_ip(si),
                    'parent_id': father_id or 'None',
                    'parent_type': parent_type,
                    'gas': gas_str,
                    'location': 'local'
                })

        # Fetch external services
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='delegated_instances';")
        if cursor.fetchone():
            cursor.execute(
                "SELECT token, peer_id, client_id, serialized_instance FROM delegated_instances"
            )
            for token, peer_id, client_id, si  in cursor.fetchall():               
                parent_type = 'client' if client_id in client_ids else 'unknown'
                instances.append({
                    'id': token,
                    'ip': get_http_ip(si),
                    'parent_id': client_id or 'N/A',
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

    def format_instance(inst, prefix=""):
        lines = [
            f"ID: {inst['id']}",
            f"IP: {inst['ip']}",
            f"Parent ID: {inst['parent_id']}",
            f"Parent Type: {inst['parent_type']}",
            f"Gas: {inst['gas']}",
            f"Location: {inst['location']}"
        ]
        for line in lines:
            print(f"{prefix}{line}")

    if groupable:
        # Build tree structure
        inst_map = {inst['id']: inst for inst in instances}
        children = {inst['id']: [] for inst in instances}
        roots = []
        for inst in instances:
            pid = inst['parent_id']
            if pid != 'None' and pid in children:
                children[pid].append(inst['id'])
            else:
                roots.append(inst['id'])

        def print_tree(node_id, prefix=""):
            inst = inst_map[node_id]
            format_instance(inst, prefix)
            print()
            for child_id in children[node_id]:
                print_tree(child_id, prefix + "    ")
                print()

        for root_id in roots:
            print_tree(root_id)
            print("------\n")
    else:
        print("Service Instances:\n")
        for inst in instances:
            format_instance(inst)
            print()
