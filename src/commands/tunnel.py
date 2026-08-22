"""``nodo tunnel <instance> <slot>`` — expose a tunnelled slot as a local port.

Binds a local listener and forwards its traffic through ``Gateway.ServiceTunnel``
streams, so an ordinary client (curl, psql, dig, a gRPC stub…) can talk to a
service that has no port of its own published — only the node's gateway port has
to be reachable.

    nodo tunnel my-instance 8080                 # via the local node
    nodo tunnel <token> 8080 --listen 9000       # on a fixed local port
    nodo tunnel <token> 5353 --udp               # datagram slot
    nodo tunnel <token> 8080 --peer 1.2.3.4:8090 # via a remote node

The listener binds to loopback by default — it is a local entry point to a
remote service, not a new way to expose one.

``--udp`` selects the *local* socket type; the node picks the node-to-service
transport from what the slot declares, so the two must match for the tunnel to
make sense end to end. The relay engine itself lives in
``src/tunneling/tunnel_client.py``, shared with the gateway's own use of it.
"""

import socket
from typing import Optional

from src.manager.manager import resolve_instance_token
from src.tunneling.tunnel_client import (
    DEFAULT_UDP_IDLE_TIMEOUT_S,
    serve_tcp,
    serve_udp,
)
from src.utils.config import ConfigManager

env_manager = ConfigManager()

DEFAULT_LISTEN_HOST = "127.0.0.1"


def _print(message: str) -> None:
    print(message, flush=True)


def tunnel(
    instance: str,
    slot: int,
    listen_port: Optional[int] = None,
    listen_host: str = DEFAULT_LISTEN_HOST,
    peer: Optional[str] = None,
    udp: bool = False,
    idle_timeout: float = DEFAULT_UDP_IDLE_TIMEOUT_S,
) -> None:
    """Serve a local port that tunnels to ``slot`` of ``instance``.

    ``instance`` may be a local instance name or id when tunnelling through the
    local node; with ``--peer`` it must be the token as the remote node knows it,
    since only that node can resolve it.
    """
    gateway = peer or f"localhost:{env_manager.get_gateway_port()}"

    if peer:
        token = instance
    else:
        token = resolve_instance_token(reference=instance, allow_uri_fallback=True) or instance

    listener = socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    )
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((listen_host, listen_port or 0))
    except OSError as e:
        _print(f"Error: cannot bind {listen_host}:{listen_port or 0} -> {e}")
        listener.close()
        return

    if not udp:
        listener.listen(16)

    bound_host, bound_port = listener.getsockname()
    transport = "udp" if udp else "tcp"

    _print(f"Tunnel listening on {bound_host}:{bound_port}/{transport}")
    _print(f"  -> slot {slot} of {token} via {gateway}")
    _print("Press Ctrl-C to stop.")

    try:
        if udp:
            serve_udp(
                listener=listener,
                token=token,
                slot=slot,
                gateway=gateway,
                idle_timeout=idle_timeout,
                log=_print,
            )
        else:
            serve_tcp(
                listener=listener,
                token=token,
                slot=slot,
                gateway=gateway,
                log=_print,
            )

    except KeyboardInterrupt:
        _print("\nStopping tunnel.")

    finally:
        listener.close()
