import sqlite3, os
from src.utils.env import EnvManager
from protos import celaut_pb2 as celaut

env_manager = EnvManager()
DATABASE_FILE = env_manager.get_env("DATABASE_FILE")
METADATA = env_manager.get_env("METADATA_REGISTRY")

def list_instances(groupable: bool = False):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    instances = []
    try:
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
        
        def get_tag(service_id: str) -> str:
            metadata = celaut.Metadata()
            try:
                with open(os.path.join(METADATA, service), "rb") as f:
                    metadata.ParseFromString(f.read())
                name = metadata.hashtag.tag[0] if metadata.hashtag.tag else service_id
                return name or service_id
            except FileNotFoundError:
                return service_id
                    
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='local_instances';")
        if cursor.fetchone():
            cursor.execute(
                "SELECT id, father_id, gas_mantissa, gas_exponent, serialized_instance, service_id FROM local_instances"
            )
            for id_, father_id, gm, ge, si, service in cursor.fetchall():        
                parent_type = (
                    'internal_service' if father_id in internal_ids else
                    'client' if father_id in client_ids else
                    'unknown'
                )
                instances.append({
                    'id': id_,
                    'service': get_tag(service),
                    'ip': get_http_ip(si),
                    'parent_id': father_id or 'None',
                    'parent_type': parent_type,
                    'gas': f"{gm * (10 ** ge):.6e}",
                    'location': 'local'
                })

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='delegated_instances';")
        if cursor.fetchone():
            cursor.execute(
                "SELECT token, peer_id, father_id, serialized_instance, service_id FROM delegated_instances"
            )
            for token, peer_id, father_id, si, service in cursor.fetchall():               
                parent_type = 'client' if father_id in client_ids else 'unknown'
                instances.append({
                    'id': token,
                    'service': get_tag(service),
                    'ip': get_http_ip(si),
                    'parent_id': father_id or 'N/A',
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
        color = '\033[90m' if inst['location'] != 'local' else ''
        reset = '\033[0m' if color else ''
        def format_line(label, value):
            lines = str(value).splitlines()
            first = f"{prefix}{label}: {lines[0]}" if lines else f"{prefix}{label}: "
            rest = [f"{prefix}    {line}" for line in lines[1:]]
            return [first] + rest

        output_lines = []
        for key in ['ID','Service','API','Parent ID','Parent Type','Gas','Location']:
            output_lines += format_line(key, inst[key.lower().replace(' ', '_')])
        for line in output_lines:
            print(f"{color}{line}{reset}")

    if groupable:
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
            format_instance(inst_map[node_id], prefix)
            if children.get(node_id):
                print("Dependencies:")
                print()
                for cid in children[node_id]:
                    print_tree(cid, prefix + "    ")
                    print()

        for rid in roots:
            print_tree(rid)
            print("-" * 40 + "\n")
    else:
        for inst in instances:
            format_instance(inst)
            print("-" * 40 + "\n")
