from typing import Any, Tuple, Optional
from pyngrok import ngrok
import urllib.parse

from src.gateway.utils import generate_gateway_instance
from src.utils.env import GATEWAY_PORT, GET_ENV
from src.utils.logger import LOGGER
from src.utils.singleton import Singleton
from protos import celaut_pb2 as celaut


class Provider:
    def __init__(self, name: str, auth_token: str, max_instances: int):
        self.name = name
        self.auth_token = auth_token
        self.max_instances = max_instances
        self.current_tunnels: List[Tuple[str, int]] = []

    def can_add_tunnel(self) -> bool:
        # Check if provider can add a new tunnel
        return len(self.current_tunnels) < self.max_instances

    def add_tunnel(self, tunnel: Tuple[str, int]) -> None:
        # Add a tunnel to the provider's list
        if self.can_add_tunnel():
            self.current_tunnels.append(tunnel)

    def remove_tunnel(self, tunnel: Tuple[str, int]) -> None:
        # Remove a tunnel from the provider's list
        if tunnel in self.current_tunnels:
            self.current_tunnels.remove(tunnel)

class TunnelSystem(metaclass=Singleton):
    def __init__(self) -> None:
        self.providers: Dict[str, Provider] = {}
        self.active_provider: Optional[Provider] = None

        # Load Ngrok tokens from environment and create providers
        self._initialize_providers()

    def _initialize_providers(self) -> None:
        # Get the Ngrok auth tokens from environment variables
        tokens = [("2gbYS8S5lwSqrmzNS5aUZD4d0NB_5Xx88jib9ohb8GCfBxCVx", 3)]

        for i, token_t in enumerate(tokens):
            token, max_i = token_t
            token = token.strip()
            if token:
                provider_name = f"ngrok_{i+1}"
                self.providers[provider_name] = Provider(
                    name=provider_name,
                    auth_token=token,
                    max_instances=max_i
                )
                LOGGER(f"Added provider: {provider_name}")

    def select_provider(self, name: str) -> None:
        # Select the active provider for tunnel operations
        if name in self.providers:
            self.active_provider = self.providers[name]
            ngrok.set_auth_token(self.active_provider.auth_token)
            LOGGER(f"Selected provider: {name}")

    def generate_tunnel(self, ip: str, port: int) -> Optional[Tuple[str, int]]:
        if not self.active_provider or not self.active_provider.can_add_tunnel():
            LOGGER("No available provider or maximum instances reached.")
            return None

        _ip, _port = ip, port
        try:
            listener = ngrok.connect(f"{_ip}:{_port}", proto="tcp")
            LOGGER(f"Ingress established at: {listener.public_url} for the service slot at uri: {_ip}:{_port}")
            _ip = listener.public_url.split("://")[1].split(":")[0]
            _port = int(listener.public_url.split("://")[1].split(":")[1])
            self.active_provider.add_tunnel((_ip, _port))
        except Exception as e:
            LOGGER(f"Exception in Ngrok module: {str(e)}.")
        return _ip, _port

    def close_tunnel(self, tunnel: Tuple[str, int]) -> None:
        if self.active_provider:
            self.active_provider.remove_tunnel(tunnel)
            LOGGER(f"Closed tunnel: {tunnel}")

    def get_url(self) -> Optional[str]:
        if self.active_provider:
            return f"{self.gat_ip}:{self.gat_port}"
        return None

    def from_tunnel(self, ip: str) -> bool:
        """
        Checks if the given IP address is from a tunnel by verifying if it is a localhost address.
        Supports both IPv4 and IPv6 formats.

        Parameters:
        ip (str): The IP address to check.

        Returns:
        bool: True if the IP is a localhost address, otherwise False.
        """
        return urllib.parse.unquote(ip) in ['127.0.0.1', '[::1]']

    def get_gateway_tunnel(self) -> Optional[Any]:
        if not self.active_provider:
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
