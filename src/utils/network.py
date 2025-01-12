import socket

def get_free_port() -> int:
    with socket.socket() as s:
        s.bind(('', 0))
        return int(s.getsockname()[1])
    
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
        print(f"Error getting local IP: {e}")
        return None

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
