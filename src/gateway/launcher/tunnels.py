from typing import Any, Tuple
from pyngrok import ngrok
import ipaddress

from src.gateway.utils import generate_gateway_instance
from src.utils.env import GATEWAY_PORT
from src.utils.singleton import Singleton

NGROK_AUTHTOKEN="2gbYS8S5lwSqrmzNS5aUZD4d0NB_5Xx88jib9ohb8GCfBxCVx"

class TunnelSystem(metaclass=Singleton):

    def __init__(self) -> None:
        self.gat_ip, self.gat_port = self.generate_tunnel("localhost", GATEWAY_PORT)
        ngrok.set_auth_token(NGROK_AUTHTOKEN)

    def from_tunnel(self, ip: str) -> bool:
        print(f" From tunnel {ip} { ip == ipaddress.IPv4Address('127.0.0.1') or ip == ipaddress.IPv6Address('::1')}")
        # If it's localhost, it's from the ngrok service. If are from docker network or outside, are self executed services and clients from internal networks without tunneling.
        return ip == ipaddress.IPv4Address('127.0.0.1') or ip == ipaddress.IPv6Address('::1')

    def generate_tunnel(self, ip: str, port: int) -> Tuple[str, int]:
        _ip, _port = ip, port
        try:
            listener = ngrok.connect(f"{_ip}:{_port}", proto="tcp")
            print(f"Ingress established at: {listener.public_url} for the service slot at uri: {_ip}:{_port}")
            _ip = listener.public_url.split("://")[1].split(":")[0]
            _port = int(listener.public_url.split("://")[1].split(":")[1])
        except Exception as e:
            print(f"Excepción en módulo de ngrok {str(e)}.")
        return _ip, _port

    def get_gateway_tunnel(self) -> Any:
        _gi = generate_gateway_instance("localhost")
        _gi.uri_slot[0].uris[0].ip = self.gat_ip
        _gi.uri_slot[0].uris[0].port = self.gat_port
        return _gi
