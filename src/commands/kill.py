"""``nodo kill <instance> [--json]`` -- stop one instance, and the tunnels to it.

A tunnel to a stopped instance keeps its local port bound and fails every new
connection, so the tunnels this host opened to the instance are closed with it --
the same close ``nodo tunnel_close`` does (``src/utils/tunnel_registry.py``).
"""

import os
from typing import Any, Dict, List

from src.manager.manager import resolve_instance_token, stop_instance
from src.utils import tunnel_registry


def close_instance_tunnels(references) -> Dict[str, List[str]]:
    """Close the tunnels on this host that reach the instance ``references`` names."""
    closed, failed = [], []
    for record in tunnel_registry.for_instance(references):
        (closed if tunnel_registry.close(record) else failed).append(record["id"])
    return {"closed": closed, "failed": failed}


def kill(instance: str, as_json: bool = False) -> bool:
    from src.commands._catalogue import emit_error, emit_json

    # Check if script is run as root
    if os.geteuid() != 0:
        return emit_error(as_json, "This script requires superuser privileges. Please run with sudo.")

    # Resolved before the stop purges the row: a tunnel records the id it reached,
    # whatever the operator typed here.
    token = resolve_instance_token(reference=instance) or instance

    if stop_instance(token=token) is None:
        return emit_error(as_json, "Something was wrong.")

    tunnels = close_instance_tunnels({instance, token})
    if as_json:
        document: Dict[str, Any] = {"killed": token, "tunnels": tunnels}
        emit_json(document)
        return True

    print(f"Service instance {instance} deleted.")
    for tunnel_id in tunnels["closed"]:
        print(f"Closed tunnel {tunnel_id}, which reached it.")
    for tunnel_id in tunnels["failed"]:
        print(f"Could not close tunnel {tunnel_id}: it belongs to another user.")
    return True
