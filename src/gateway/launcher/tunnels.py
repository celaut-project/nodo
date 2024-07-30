from typing import Any, Tuple, Optional
from pyngrok import ngrok
import urllib.parse

from src.gateway.utils import generate_gateway_instance
from src.utils.env import GATEWAY_PORT, GET_ENV
from src.utils.singleton import Singleton
from protos import celaut_pb2 as celaut


NGROK_AUTH_TOKEN: Optional[str] = GET_ENV("NGROK_AUTH_TOKEN", "")


class TunnelSystem(metaclass=Singleton):

    def __init__(self) -> None:
        self.authenticated = bool(NGROK_AUTH_TOKEN)
        if self.authenticated:
            ngrok.set_auth_token(NGROK_AUTH_TOKEN)
            self.gat_ip, self.gat_port = self.generate_tunnel("localhost", GATEWAY_PORT)

    def get_url(self) -> Optional[str]:
        return f"{self.gat_ip}:{self.gat_port}" if self.authenticated else None

    def from_tunnel(self, ip: str) -> bool:
        # If it's localhost, it's from the ngrok service. If are from docker network or outside,
        # are self executed services and clients from internal networks without tunneling.
        decoded_str = ":".join(urllib.parse.unquote(ip).split(':')[1:-1])
        if decoded_str in ['127.0.0.1', '[::1]']: print(f"The ip {ip} it's from a tunnel.")
        return decoded_str in ['127.0.0.1', '[::1]']

    def generate_tunnel(self, ip: str, port: int) -> Optional[Tuple[str, int]]:
        if not self.authenticated:
            return None

        _ip, _port = ip, port
        try:
            listener = ngrok.connect(f"{_ip}:{_port}", proto="tcp")
            print(f"Ingress established at: {listener.public_url} for the service slot at uri: {_ip}:{_port}")
            _ip = listener.public_url.split("://")[1].split(":")[0]
            _port = int(listener.public_url.split("://")[1].split(":")[1])
        except Exception as e:
            print(f"Excepción en módulo de ngrok {str(e)}.")
        return _ip, _port

    def get_gateway_tunnel(self) -> Optional[Any]:
        if not self.authenticated:
            return None

        _gi = generate_gateway_instance('localhost')
        _gi.instance.uri_slot[0].uri.pop()
        _gi.instance.uri_slot[0].uri.append(
            celaut.Instance.Uri(
                ip=self.gat_ip,
                port=self.gat_port
            )
        )
        return _gi
