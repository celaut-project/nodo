"""``nodo status [--json] [--wallet] [--storage]`` -- the TUI's OVERVIEW, as data.

Bare ``nodo`` answers the same questions for a person, in prose, and it prints
them as it learns them. That is the wrong shape for a script or an AI agent: it
has no structured form, and its exit status says nothing about the node. This
command reads the same sources -- the gateway port probe, the identity key, the
announced address, ``operator_alerts``, the catalogue, the host -- and prints
them as one report, ``--json`` for one JSON object.

Two halves of the OVERVIEW are opt-in because they are slow, exactly as the TUI
refreshes them on their own clock rather than every frame:

* ``--wallet`` asks each configured payment contract for its balance (a JVM and
  the network -- the TUI does it once a minute, in the background);
* ``--storage`` walks the installation's storage directory to size it (the TUI
  does it every 30 seconds).

Exit status is 0 when the report was produced, whatever it says: "the node is
down" is an answer, not a failure of this command. Read ``serving`` for that.
"""

import os
import shutil
import sqlite3
import subprocess
from typing import Any, Dict, List, Optional


def _git_commit(main_dir: str) -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "-C", main_dir, "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip() or None
    except Exception:
        return None


def _address(env_manager, port) -> Dict[str, Any]:
    """The address peers are told -- the same resolution bare ``nodo`` prints."""
    from src.utils.network import get_local_ip, resolve_public_host, resolve_public_port

    if not port:
        return {"address": None, "scope": None,
                "reason": "network.GATEWAY_PORT is not assigned yet"}
    try:
        outbound_ip = get_local_ip()
    except Exception:
        outbound_ip = None
    public_host = resolve_public_host(
        configured=str(env_manager.get("network.PUBLIC_IP", "") or ""),
        outbound_ip=outbound_ip,
    )
    if public_host:
        public_port = resolve_public_port(env_manager.get("network.PUBLIC_TCP_PORT", ""), port)
        return {"address": f"{public_host}:{public_port}", "scope": "public"}
    if outbound_ip:
        return {"address": f"{outbound_ip}:{port}", "scope": "local network"}
    return {"address": None, "scope": None, "reason": "could not determine an address"}


def _counts(database_file: str, registry: str) -> Dict[str, Optional[int]]:
    from src.commands._catalogue import table_exists

    counts: Dict[str, Optional[int]] = {
        "local_instances": None, "delegated_instances": None,
        "peers": None, "clients": None, "services": None,
        "reserved_mem_bytes": None, "reserved_disk_bytes": None,
    }
    try:
        counts["services"] = len(os.listdir(registry))
    except OSError:
        pass
    try:
        connection = sqlite3.connect(database_file)
    except sqlite3.Error:
        return counts
    try:
        for key, table in (("local_instances", "local_instances"),
                           ("delegated_instances", "delegated_instances"),
                           ("peers", "peer"), ("clients", "clients")):
            if table_exists(connection, table):
                counts[key] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if table_exists(connection, "local_instances"):
            mem, disk = connection.execute(
                "SELECT COALESCE(SUM(mem_limit), 0), COALESCE(SUM(disk_space), 0) FROM local_instances"
            ).fetchone()
            counts["reserved_mem_bytes"], counts["reserved_disk_bytes"] = int(mem), int(disk)
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return counts


def _host(main_dir: str) -> Dict[str, Any]:
    host: Dict[str, Any] = {"cpu_count": os.cpu_count(), "load_avg": None,
                            "mem_total_bytes": None, "mem_available_bytes": None,
                            "disk_total_bytes": None, "disk_free_bytes": None}
    try:
        host["load_avg"] = list(os.getloadavg())
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    value = int(rest.split()[0]) * 1024
                    host["mem_total_bytes" if key == "MemTotal" else "mem_available_bytes"] = value
    except (OSError, ValueError, IndexError):
        pass
    try:
        usage = shutil.disk_usage(main_dir)
        host["disk_total_bytes"], host["disk_free_bytes"] = usage.total, usage.free
    except OSError:
        pass
    return host


def _directory_size(path: str) -> Optional[int]:
    total = 0
    try:
        for dirpath, _, names in os.walk(path):
            for name in names:
                try:
                    total += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
    except OSError:
        return None
    return total


def collect(wallet: bool = False, storage: bool = False) -> Dict[str, Any]:
    """Everything OVERVIEW shows, as plain data. Never raises."""
    from src.commands.daemon import is_serving
    from src.utils.config import ConfigManager

    env_manager = ConfigManager()
    main_dir = env_manager.get("MAIN_DIR")
    report: Dict[str, Any] = {}

    try:
        report["serving"] = is_serving()
    except Exception as e:
        report["serving"], report["serving_error"] = None, str(e)
    report["version"] = _git_commit(main_dir)

    try:
        from src.identity.node_identity import get_node_public_key_hex
        report["node_id"] = get_node_public_key_hex()
    except Exception as e:
        report["node_id"], report["node_id_error"] = None, str(e)

    port = env_manager.gateway_port_or_none()
    report["gateway_port"] = port
    report.update(_address(env_manager, port))
    report["reputation_proof_id"] = env_manager.get(
        "ledgers.ergo.reputation.REPUTATION_PROOF_ID") or None

    alerts: List[Dict[str, str]] = []
    try:
        from src.utils.operator_alerts import collect as collect_alerts
        for alert in collect_alerts(serving=report.get("serving")):
            alerts.append({"key": alert.key, "summary": alert.summary,
                           "detail": alert.detail, "line": alert.as_line()})
    except Exception as e:
        report["alerts_error"] = str(e)
    report["alerts"] = alerts

    report["counts"] = _counts(env_manager.get("DATABASE_FILE"), env_manager.get("REGISTRY"))
    report["host"] = _host(main_dir)

    if storage:
        report["storage_bytes"] = _directory_size(os.path.join(main_dir, "storage"))
    if wallet:
        try:
            from src.payment_system.contracts.envs import print_payment_info
            report["wallet"] = print_payment_info()
        except Exception as e:
            report["wallet"], report["wallet_error"] = None, str(e)
    return report


def _print(report: Dict[str, Any]) -> None:
    from src.commands.services import format_bytes

    def size(value):
        return format_bytes(value) if value is not None else "unknown"

    serving = report.get("serving")
    print(f"Serving:        {'yes' if serving else 'no' if serving is not None else 'unknown'}")
    print(f"Version:        {report.get('version') or 'unknown'}")
    print(f"Node id:        {report.get('node_id') or 'unavailable'}")
    print(f"Gateway port:   {report.get('gateway_port') or 'unassigned'}")
    address = report.get("address")
    print(f"Address:        {address + ' (' + report['scope'] + ')' if address else 'unavailable -- ' + report.get('reason', '')}")
    print(f"Reputation proof: {report.get('reputation_proof_id') or 'N/A'}")
    counts = report["counts"]
    print("Counts:         " + ", ".join(
        f"{key.replace('_', ' ')} {value if value is not None else '?'}"
        for key, value in counts.items() if not key.startswith("reserved")))
    print(f"Reserved:       memory {size(counts['reserved_mem_bytes'])}, "
          f"disk {size(counts['reserved_disk_bytes'])}")
    host = report["host"]
    print(f"Host:           {host['cpu_count']} CPUs, load {host['load_avg'] or '?'}, "
          f"memory {size(host['mem_available_bytes'])} free of {size(host['mem_total_bytes'])}, "
          f"disk {size(host['disk_free_bytes'])} free of {size(host['disk_total_bytes'])}")
    if "storage_bytes" in report:
        print(f"Storage:        {size(report['storage_bytes'])}")
    if "wallet" in report:
        print(report["wallet"] or f"Wallet: unavailable ({report.get('wallet_error')})")
    if report["alerts"]:
        print()
        for alert in report["alerts"]:
            print(alert["line"])


def status(argv=None) -> bool:
    from src.commands._catalogue import emit_json

    argv = list(argv or [])
    report = collect(wallet="--wallet" in argv, storage="--storage" in argv)
    if "--json" in argv:
        emit_json(report)
    else:
        _print(report)
    return True
