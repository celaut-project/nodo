from typing import Any, Tuple

from src.gateway.utils import generate_gateway_instance
from src.utils.env import GATEWAY_PORT
from src.utils.singleton import Singleton


class TunnelSystem(metaclass=Singleton):

    def __init__(self) -> None:
        self.gat_ip, self.gat_port = self.generate_tunnel("localhost", GATEWAY_PORT)

    def from_tunnel(self, ip: str, id: str) -> bool:
        return True

    def generate_tunnel(self, ip: str, port: int) -> Tuple[str, int]:
        _ip, _port = ip, port
        try:
            import ngrok
            listener = ngrok.forward(f"{_ip}:{_port}", authtoken_from_env=True, proto="tcp")
            print(f"Ingress established at: {listener.url()} for the service slot at uri: {_ip}:{_port}")
            _ip = listener.url().split("://")[1].split(":")[0]
            _port = int(listener.url().split("://")[1].split(":")[1])
        except Exception as e:
            print(f"Excepción en módulo de ngrok {str(e)}.")
        return _ip, _port

    def get_gateway_tunnel(self) -> Any:
        _gi = generate_gateway_instance("localhost")
        _gi.uri_slot[0].uris[0].ip = self.gat_ip
        _gi.uri_slot[0].uris[0].port = self.gat_port
        return _gi
