import sqlite3
import os
from src.utils.env import EnvManager
from protos import celaut_pb2 as celaut

env_manager = EnvManager()
DATABASE_FILE = env_manager.get_env("DATABASE_FILE")
METADATA = env_manager.get_env("METADATA_REGISTRY")

def list_instances(groupable: bool = False, search: str = ""):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    instances = []
    try:
        cursor.execute("SELECT id FROM local_instances;")
        internal_ids = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT id FROM clients;")
        client_ids = {row[0] for row in cursor.fetchall()}

        def get_http_ip(serialized_instance: bytes) -> str:
            try:
                instance = celaut.Instance()
                instance.ParseFromString(serialized_instance)
                s = ""
                for _exp in instance.uri_slot:
                    for _uri in _exp.uri:
                        s += f"\n  • {_uri.ip}:{_uri.port}  (#{_exp.internal_port})"
                return s.strip() if s else "N/A"
            except Exception:
                return "Error Parsing Instance"

        def get_tag(service_id: str) -> str:
            if not service_id: return "Unknown Service ID"
            metadata = celaut.Metadata()
            try:
                metadata_path = os.path.join(METADATA, service_id)
                with open(metadata_path, "rb") as f:
                    metadata.ParseFromString(f.read())
                name = metadata.hashtag.tag[0] if metadata.hashtag.tag else service_id
                return name or service_id
            except FileNotFoundError:
                return service_id
            except Exception:
                return f"{service_id} (Metadata Error)"

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
                gas_value = 'N/A'
                if gm is not None and ge is not None:
                   try:
                       gas_value = f"{float(gm) * (10 ** int(ge)):e}"
                   except (ValueError, TypeError):
                       gas_value = "Invalid Gas Data"

                instances.append({
                    'id': id_ or 'N/A',
                    'service': get_tag(service),
                    'ip': get_http_ip(si) if si else "N/A",
                    'parent_id': father_id or 'None',
                    'parent_type': parent_type,
                    'gas': gas_value,
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
                    'id': token or 'N/A',
                    'service': get_tag(service),
                    'ip': get_http_ip(si) if si else "N/A",
                    'parent_id': father_id or 'N/A',
                    'parent_type': parent_type,
                    'gas': 'N/A',
                    'location': peer_id or 'Unknown Peer'
                })

    except sqlite3.Error as e:
        print(f"An error occurred while retrieving instances: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        conn.close()

    if search:
        filtered_instances = []
        search_lower = search.lower()
        for inst in instances:
            combined_data = " ".join(str(v).lower() for v in inst.values())
            if search_lower in combined_data:
                filtered_instances.append(inst)
        instances = filtered_instances

    if not instances:
        if search:
            print(f"No service instances found matching '{search}'.")
        else:
            print("No service instances found.")
        return

    def format_instance(inst, prefix=""):
        color = '\033[37m' if inst.get('location', 'local') != 'local' else ''
        reset = '\033[0m' if color else ''

        def format_line(label, value):
            lines = str(value).splitlines()
            first_line = lines[0] if lines else ""
            first = f"{prefix}{label}: {first_line}"
            rest = [f"{prefix}    {line}" for line in lines[1:]]
            return [first] + rest

        fields = [
            ("ID", "id"),
            ("Service", "service"),
            ("API", "ip"),
            ("Parent ID", "parent_id"),
            ("Parent Type", "parent_type"),
            ("Gas", "gas"),
            ("Location", "location"),
        ]
        output_lines = []
        for label, key in fields:
            output_lines.extend(format_line(label, inst.get(key, 'N/A')))

        for line in output_lines:
            print(f"{color}{line}{reset}")

    if groupable:
        inst_map = {inst['id']: inst for inst in instances if inst.get('id') != 'N/A'}
        children = {inst_id: [] for inst_id in inst_map.keys()}
        roots = []
        processed_ids = set()

        for inst_id, inst in inst_map.items():
            pid = inst.get('parent_id')
            if pid != 'None' and pid != 'N/A' and pid in inst_map:
                if pid not in children:
                    children[pid] = []
                children[pid].append(inst_id)
                processed_ids.add(inst_id)

        for inst_id in inst_map.keys():
            if inst_id not in processed_ids:
                pid = inst_map[inst_id].get('parent_id')
                if pid == 'None' or pid == 'N/A' or pid not in inst_map:
                    roots.append(inst_id)

        printed_nodes = set()
        def print_tree(node_id, prefix=""):
            if node_id in printed_nodes: return
            printed_nodes.add(node_id)

            if node_id not in inst_map: return

            format_instance(inst_map[node_id], prefix)
            node_children = children.get(node_id, [])
            if node_children:
                print(f"{prefix}Dependencies:")
                print()
                for i, cid in enumerate(node_children):
                    print_tree(cid, prefix + "    ")
                    if i < len(node_children) - 1:
                        print()

        for i, rid in enumerate(roots):
            print_tree(rid)
            if i < len(roots) - 1:
                print("\n" + "-" * 40 + "\n")

    else:
        for i, inst in enumerate(instances):
            format_instance(inst)
            if i < len(instances) - 1:
                print("-" * 40 + "\n")