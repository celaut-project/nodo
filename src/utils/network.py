import socket, subprocess

def get_free_port(open_port: bool = False) -> int:
    """
    Finds a free port on the system. If open_port is True, it attempts to open
    the found port in the firewall using ufw (Linux).

    Args:
        open_port (bool): If True, attempts to open the found port in the firewall
                           using ufw. This might require root privileges on Linux.

    Returns:
        int: A free port number.
    """
    with socket.socket() as s:
        s.bind(('', 0))
        port = int(s.getsockname()[1])
        if open_port:
            try:
                subprocess.run(['ufw', 'allow', str(port) + '/tcp'], check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                raise Exception(f"Error attempting to open port {port} in the firewall (ufw): {e.stderr}")
            except FileNotFoundError:
                raise Exception("ufw command not found. Ensure ufw is installed if you intend to open ports.")
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
        raise(f"Error getting local IP: {e}")

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
