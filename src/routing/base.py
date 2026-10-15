"""The interface every routing backend implements."""

import abc
import socket
from typing import List, Tuple


class RoutingUnavailable(ConnectionError):
    """The backend an address needs is not running on this node.

    Raised instead of falling back to a direct connection: an address that has to go
    through a backend must never be resolved or dialled outside it.
    """


class RoutingBackend(abc.ABC):
    #: Short name, for logs and ``nodo tor status``.
    name: str = ""
    #: The ``Peer.Uri.transport`` tag of the addresses this backend reaches.
    tag: str = ""
    #: Timeout of the TLS pre-check that reads a peer's certificate.
    preflight_timeout_s: float = 5.0
    #: Timeout and cache lifetimes of the reachability probe (``utils.is_open``).
    probe_timeout_s: float = 1.0
    probe_ttl_open_s: float = 30.0
    probe_ttl_closed_s: float = 30.0

    @abc.abstractmethod
    def handles_host(self, host: str) -> bool:
        """Whether ``host`` can only be reached through this backend."""

    def valid_host(self, host: str) -> bool:
        """Whether ``host`` is a well-formed address of this network (checked on intake)."""
        return True

    @abc.abstractmethod
    def available(self) -> bool:
        """Whether a call can go through this backend right now."""

    @abc.abstractmethod
    def open_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        """A connected stream socket to ``host:port`` through this backend."""

    @abc.abstractmethod
    def grpc_channel_options(self, host: str) -> List[Tuple[str, object]]:
        """Channel options that make a gRPC channel to ``host`` use this backend."""

    def start(self) -> None:
        """Bring the backend up. Idempotent. Must not raise."""

    def stop(self) -> None:
        """Take the backend down. Idempotent. Must not raise."""
