import sqlite3
import os
from src.manager.metrics import __get_metrics_external
from src.utils.config import ConfigManager
from protos import celaut_pb2 as celaut
from src.utils.logger import ssformat
from src.utils.monetary import format_mu
from src.utils.utils import from_amount
try:
    from src.virtualizers.microvm.observability import get_vm_runtime_snapshot
except Exception:  # pragma: no cover - defensive fallback for minimal environments
    def get_vm_runtime_snapshot(vmachine_id: str):
        _ = vmachine_id
        return {
            "pid": None,
            "alive": False,
            "uptime_s": None,
            "mem_rss_bytes": None,
            "cgroup_memory_max_raw": None,
            "cgroup_memory_max_bytes": None,
            "cgroup_memory_current_bytes": None,
            "log_paths": {},
        }

env_manager = ConfigManager()
DATABASE_FILE = env_manager.get("DATABASE_FILE")
METADATA = env_manager.get("METADATA_REGISTRY")
DEFAULT_VIRTUALIZER = env_manager.get("virtualizers.DEFAULT_VIRTUALIZER", "ch")

def _direct_purge_local_instance(instance_id: str) -> None:
    conn = sqlite3.connect(DATABASE_FILE)
    try:
        conn.execute("DELETE FROM local_instances WHERE id = ?", (instance_id,))
        conn.commit()
    finally:
        conn.close()

def _has_runtime_snapshot(virtualizer) -> bool:
    """Does this instance's backend have a local process to snapshot at all?

    The PID, uptime, RSS and cgroup figures below are read off a hypervisor
    process and a cgroup on this host, so they exist for the microVM family and
    for nothing else -- a backend running the guest somewhere else has no such
    process to report. Asked of the registry rather than compared against the
    string ``"ch"``, which is what hid every QEMU instance's runtime column.
    """
    from src.virtualizers.registry import MICROVM, family_of

    try:
        return family_of(virtualizer or DEFAULT_VIRTUALIZER).name == MICROVM
    except Exception:
        return False

def _instance_is_stale(instance_id: str) -> bool:
    """Is this row's guest gone, judged by what the launcher actually recorded?

    Matched against the recorded process name and the recorded control socket, so
    the same three checks answer for any backend that wrote them. This used to
    read CH's socket key with CH's process matcher, and to skip every instance
    whose ``virtualizer`` column was not ``ch`` -- so a dead QEMU guest stayed
    listed as running forever.
    """
    try:
        from src.virtualizers.microvm.process import pid_alive
        from src.virtualizers.microvm.runtime_state import (
            load_runtime_state,
            recorded_process_name,
        )

        state = load_runtime_state(instance_id)
        if not state:
            return True
        pid = int(state.get("pid") or 0)
        if pid <= 0 or not pid_alive(pid=pid, process_name=recorded_process_name(state)):
            return True
        control_socket = str(state.get("control_socket") or "").strip()
        return bool(control_socket) and not os.path.exists(control_socket)
    except Exception:
        return False

def _prune_stale_instances() -> None:
    conn = sqlite3.connect(DATABASE_FILE)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM local_instances")
        candidates = [row[0] for row in cursor.fetchall()]
    except sqlite3.Error:
        return
    finally:
        conn.close()

    for instance_id in candidates:
        if not _instance_is_stale(instance_id):
            continue
        try:
            from src.manager.manager import stop_instance

            if stop_instance(token=instance_id) is None:
                _direct_purge_local_instance(instance_id)
        except Exception:
            # `stop_instance` is the normal teardown; this is the fallback for a
            # row it could not act on. Routed through the interface so the kill
            # belongs to whichever backend launched the guest.
            try:
                from src.virtualizers.interface import kill as vm_kill

                vm_kill(vmachine_id=instance_id)
            except Exception:
                pass
            _direct_purge_local_instance(instance_id)

def list_instances(groupable: bool = False, search: str = ""):
    _prune_stale_instances()

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
            
        def bytes_to_readable(bytes_value):
            if bytes_value is None:
                return "N/A"
            # Define units and their thresholds (using 1024-based units)
            units = [(1024**3, "GB"), (1024**2, "MB"), (1024, "KB"), (1, "bytes")]
            
            # Find the appropriate unit
            for threshold, unit in units:
                if bytes_value >= threshold:
                    value = bytes_value / threshold
                    # Return formatted string (2 decimal places unless it's bytes)
                    return f"{value:.2f} {unit}" if unit != "bytes" else f"{int(value)} {unit}"

            return "0 bytes"  # Fallback for zero bytes

        def cgroup_limit_to_readable(limit_raw, limit_bytes):
            if str(limit_raw or "").strip() == "max":
                return "max"
            return bytes_to_readable(limit_bytes)

        def seconds_to_readable(seconds_value):
            if seconds_value is None:
                return "N/A"
            try:
                total_seconds = int(seconds_value)
            except (TypeError, ValueError):
                return "N/A"
            if total_seconds < 0:
                total_seconds = 0
            days, rem = divmod(total_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            if minutes:
                parts.append(f"{minutes}m")
            parts.append(f"{seconds}s")
            return " ".join(parts)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='local_instances';")
        if cursor.fetchone():
            cursor.execute(
                "SELECT id, name, father_id, balance_mu, serialized_instance, service_id, mem_limit, disk_space, virtualizer FROM local_instances"
            )
            for id_, name, father_id, balance_mu, si, service, mem_limit, disk_space, virtualizer in cursor.fetchall():
                parent_type = (
                    'internal_service' if father_id in internal_ids else
                    'client' if father_id in client_ids else
                    'unknown'
                )
                runtime_virtualizer = str(virtualizer).strip() if virtualizer else DEFAULT_VIRTUALIZER
                try:
                    balance_value = f"{format_mu(int(balance_mu))}"
                except (ValueError, TypeError):
                    balance_value = "Invalid balance"

                vm_pid = "N/A"
                vm_uptime = "N/A"
                vm_mem_rss = "N/A"
                vm_mem_limit_cgroup = "N/A"
                vm_mem_current_cgroup = "N/A"
                if groupable and _has_runtime_snapshot(runtime_virtualizer) and id_:
                    snapshot = get_vm_runtime_snapshot(vmachine_id=id_)
                    vm_pid = str(snapshot.get("pid")) if snapshot.get("pid") is not None else "N/A"
                    vm_uptime = seconds_to_readable(snapshot.get("uptime_s"))
                    vm_mem_rss = bytes_to_readable(snapshot.get("mem_rss_bytes"))
                    vm_mem_limit_cgroup = cgroup_limit_to_readable(
                        snapshot.get("cgroup_memory_max_raw"),
                        snapshot.get("cgroup_memory_max_bytes"),
                    )
                    vm_mem_current_cgroup = bytes_to_readable(snapshot.get("cgroup_memory_current_bytes"))

                instances.append({
                    'id': id_ or 'N/A',
                    'name': name or 'N/A',
                    'external_token': 'N/A',
                    'service': get_tag(service),
                    'ip': get_http_ip(si) if si else "N/A",
                    'parent_id': father_id or 'None',
                    'parent_type': parent_type,
                    'balance': balance_value,
                    'location': 'local',
                    'virtualizer': runtime_virtualizer,
                    'mem_limit': bytes_to_readable(mem_limit),
                    'disk_space': bytes_to_readable(disk_space),
                    'vm_pid': vm_pid,
                    'vm_uptime': vm_uptime,
                    'vm_mem_rss': vm_mem_rss,
                    'vm_mem_limit_cgroup': vm_mem_limit_cgroup,
                    'vm_mem_current_cgroup': vm_mem_current_cgroup,
                })

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='delegated_instances';")
        if cursor.fetchone():
            cursor.execute(
                "SELECT token_delegation, id, peer_id, father_id, serialized_instance, service_id FROM delegated_instances"
            )
            for external_token, id, peer_id, father_id, si, service in cursor.fetchall():
                parent_type = 'client' if father_id in client_ids else 'unknown'

                try:
                    metrics = __get_metrics_external(token=external_token, peer_id=peer_id)
                    balance_value = f"{format_mu(from_amount(metrics.balance))}"
                except:
                    balance_value = "N/A"
                
                instances.append({
                    'id': id or 'N/A',
                    'external_token': external_token,
                    'service': get_tag(service),
                    'ip': get_http_ip(si) if si else "N/A",
                    'parent_id': father_id or 'N/A',
                    'parent_type': parent_type,
                    'balance': balance_value,
                    'location': peer_id or 'Unknown Peer',
                    'virtualizer': 'delegated',
                    'mem_limit': 'N/A',
                    'disk_space': 'N/A',
                    'vm_pid': 'N/A',
                    'vm_uptime': 'N/A',
                    'vm_mem_rss': 'N/A',
                    'vm_mem_limit_cgroup': 'N/A',
                    'vm_mem_current_cgroup': 'N/A',
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

    def format_instance(inst, prefix="", include_runtime=False):
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
            ("Name", "name"),
            ("Service", "service"),
            ("External token", "external_token"),
            ("API", "ip"),
            ("Parent ID", "parent_id"),
            ("Parent Type", "parent_type"),
            ("Balance", "balance"),
            ("Location", "location"),
            ("Virtualizer", "virtualizer"),
            ("Memory limit", "mem_limit"),
            ("Disk limit", "disk_space"),
        ]
        if include_runtime and _has_runtime_snapshot(inst.get("virtualizer")):
            fields.extend(
                [
                    ("VM PID", "vm_pid"),
                    ("VM Uptime", "vm_uptime"),
                    ("VM Memory (RSS)", "vm_mem_rss"),
                    ("VM Memory limit (cgroup)", "vm_mem_limit_cgroup"),
                    ("VM Memory current (cgroup)", "vm_mem_current_cgroup"),
                ]
            )
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

            format_instance(inst_map[node_id], prefix, include_runtime=True)
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
            format_instance(inst, include_runtime=False)
            if i < len(instances) - 1:
                print("-" * 40 + "\n")
