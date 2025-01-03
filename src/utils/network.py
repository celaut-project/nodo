import socket
from src.utils.env import EnvManager

env_manager = EnvManager()

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

def available_ergo_node() -> bool:
    ergo_node_url = env_manager.get_env("ERGO_NODE_URL")
    _ip, _port = ergo_node_url.split(":")
    try:
        response = socket.create_connection((_ip, _port), timeout=5)
        response.close()
        return True
    except Exception as e:
        print(f"Error connecting to Ergo node: {e}")
        return False
