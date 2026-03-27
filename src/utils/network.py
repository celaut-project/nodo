import socket, subprocess, os
from src.utils.config import ConfigManager

env_manager = ConfigManager()

import os
import random
import socket
import subprocess


def _is_port_free(port: int) -> bool:
    """Checks whether a TCP port is available on the local machine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            return True
        except OSError:
            return False


def get_free_port() -> int:
    """
    Finds a free port on the system.

    If network.FREE_PORTS_RANGE is configured, picks a free port within those ranges.
    Otherwise, falls back to any free port assigned by the OS.
    """
    free_port_ranges = env_manager.get("network.FREE_PORTS_RANGE", [])

    port = None

    # Try configured ranges first
    if free_port_ranges:
        candidates = []
        for r in free_port_ranges:
            start = int(r["START"])
            end = int(r["END"])
            if start > end:
                continue
            candidates.extend(range(start, end + 1))

        random.shuffle(candidates)

        for candidate in candidates:
            if _is_port_free(candidate):
                port = candidate
                break

        if port is None:
            raise RuntimeError("No free port found within configured FREE_PORTS_RANGE.")

    # OS choose any free port in case no free port range is configured.
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            port = int(s.getsockname()[1])

    return port
    
def get_local_ip() -> str:
    try:
        # Se conecta a un servidor remoto para determinar la IP de salida
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        # 8.8.8.8 es un servidor DNS de Google, y el puerto 80 es el estándar para HTTP.
        s.connect(('8.8.8.8', 80))
        ip_address = s.getsockname()[0]
        s.close()
        return ip_address
    except Exception as e:
        raise(f"Error getting local IP: {e}") # pyright: ignore[reportGeneralTypeIssues]

def internet_available() -> bool:
    """
    Check if the internet is available by attempting to resolve multiple host names.

    Returns:
        bool: True if at least one host is reachable, False otherwise.
    """
    # List of hostnames to check
    hosts = [
        "python.org",
        "rust-lang.org",
        "linux.org",
        "ergoplatform.org",
        "sigmaspace.io"
    ]
    
    for host in hosts:
        try:
            # Try connecting to the host on port 80 (HTTP)
            socket.create_connection((host, 80), timeout=5)
            return True  # Internet is available if at least one host is reachable
        except (socket.gaierror, socket.timeout):
            continue  # Try the next host
    
    return False  # Internet is not available if no hosts are reachable
