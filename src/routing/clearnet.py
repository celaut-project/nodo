"""The default backend: a direct TCP connection."""

import socket
from typing import List, Tuple

from src.routing.base import RoutingBackend


class ClearnetBackend(RoutingBackend):
    name = "clearnet"
    tag = "tcp"

    def handles_host(self, host: str) -> bool:
        return False

    def available(self) -> bool:
        return True

    def open_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return socket.create_connection((host, port), timeout=timeout)

    def grpc_channel_options(self, host: str) -> List[Tuple[str, object]]:
        return []
