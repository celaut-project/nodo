from typing import Any, Tuple, Optional, Dict, List
from pyngrok import ngrok
import urllib.parse
import random, socket

from src.gateway.utils import generate_gateway_instance
from src.utils.env import GATEWAY_PORT, GET_ENV
from src.utils.logger import LOGGER
from src.utils.singleton import Singleton
from protos import celaut_pb2 as celaut


NUM_GATEWAY_TUNNELS = 1

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
        self.current_tunnels.append(tunnel)

    def remove_tunnel(self, tunnel: Tuple[str, int]) -> None:
        # Remove a tunnel from the provider's list
        if tunnel in self.current_tunnels:
            self.current_tunnels.remove(tunnel)

    def is_tunnel_active(self, tunnel: Tuple[str, int]) -> bool:
        try:
            ip, port = tunnel
            with socket.create_connection((ip, port), timeout=5):
                return True
        except OSError:
            return False

class TunnelSystem(metaclass=Singleton):
    def __init__(self) -> None:
        self.providers: Dict[str, Provider] = {}
        self.gateway_tunnels: List[Tuple[str, int]] = []

        # Load Ngrok tokens from environment and create providers
        self.__initialize_providers()

    def __initialize_providers(self) -> None:
        # Get the Ngrok auth tokens from environment variables
        ngrok_key = GET_ENV("NGROK_TUNNELS_KEY", "")
        tokens = [(ngrok_key, 3)] if ngrok_key else []

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

    def __select_provider(self) -> Optional[str]:
        # Select the active provider for tunnel operations
        for name, provider in self.providers.items():
            if provider.can_add_tunnel():
                ngrok.set_auth_token(provider.auth_token)
                return name
        LOGGER("No available provider or maximum instances reached.")

    def generate_tunnel(self, ip: str, port: int) -> Optional[Tuple[str, int]]:
        _ip, _port = ip, port
        provider = self.__select_provider()
        try:
            listener = ngrok.connect(f"{_ip}:{_port}", proto="tcp")
            LOGGER(f"""Ingress established at: {listener.public_url} for the
                service slot at uri: {_ip}:{_port} using provider {provider}""")
            _ip = listener.public_url.split("://")[1].split(":")[0]
            _port = int(listener.public_url.split("://")[1].split(":")[1])  # typed: ignore
            self.providers[provider].add_tunnel((_ip, _port))
            return _ip, _port
        except Exception as e:
            LOGGER(f"Exception in Ngrok module: {str(e)}.")

    def close_tunnel(self, provider: str, tunnel: Tuple[str, int]):
        if provider in self.providers:
            self.providers[provider].remove_tunnel(tunnel)
            LOGGER(f"Closed tunnel: {tunnel}")
        else:
            LOGGER(f"Provider {provider} not found.")

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

    def __generate_gateway_tunnel(self):
        _r = NUM_GATEWAY_TUNNELS - len(self.gateway_tunnels)
        if _r:
            LOGGER("Generate gateway tunnels.")
            for i in range(0, _r):
                tunnel = self.generate_tunnel("localhost", GATEWAY_PORT)
                if tunnel:
                    self.gateway_tunnels.append(tunnel)

    def get_gateway_urls(self) -> List[str]:
        if not self.gateway_tunnels:
            self.__generate_gateway_tunnel()
        return [f"{ip}:{port}" for ip, port in self.gateway_tunnels]


    def get_gateway_tunnel(self) -> Optional[Any]:
        _gi = generate_gateway_instance(network='localhost')
        _gi.instance.uri_slot[0].uri.pop()

        if not self.gateway_tunnels:
            self.__generate_gateway_tunnel()

        for gat_ip, gat_port in self.gateway_tunnels:
            _gi.instance.uri_slot[0].uri.append(
                celaut.Instance.Uri(
                    ip=gat_ip,
                    port=gat_port
                )
            )
        return _gi
