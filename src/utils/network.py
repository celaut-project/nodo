import random
import socket


def _is_port_free(port: int) -> bool:
    """Checks whether a TCP port is available on the local machine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            return True
        except OSError:
            return False


def get_free_port(free_port_ranges=None) -> int:
    """
    Finds a free port on the system.

    If free_port_ranges is provided, picks a free port within those ranges.
    Otherwise, falls back to any free port assigned by the OS.
    """
    port = None

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
        s.connect(("8.8.8.8", 80))
        ip_address = s.getsockname()[0]
        s.close()
        return ip_address
    except Exception as e:
        raise (f"Error getting local IP: {e}")  # pyright: ignore[reportGeneralTypeIssues]


def internet_available() -> bool:
    """
    Check if the internet is available by attempting to resolve multiple host names.

    Returns:
        bool: True if at least one host is reachable, False otherwise.
    """
    hosts = [
        "python.org",
        "rust-lang.org",
        "linux.org",
        "ergoplatform.org",
        "sigmaspace.io",
    ]

    for host in hosts:
        try:
            socket.create_connection((host, 80), timeout=5)
            return True
        except (socket.gaierror, socket.timeout):
            continue

    return False
