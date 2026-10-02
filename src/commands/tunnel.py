"""``nodo tunnel <instance> <slot>`` — expose a tunnelled slot as a local port.

Binds a local listener and forwards its traffic through ``Gateway.ServiceTunnel``
streams, so an ordinary client (curl, psql, dig, a gRPC stub…) can talk to a
service that has no port of its own published — only the node's gateway port has
to be reachable.

    nodo tunnel my-instance 8080                 # via the local node
    nodo tunnel <token> 8080 --listen 9000       # on a fixed local port
    nodo tunnel <token> 5353 --udp               # datagram slot
    nodo tunnel <token> 8080 --peer 1.2.3.4:8090 # via a remote node
    nodo tunnel my-instance 8080 --detach --json # in the background, for scripts

Every tunnel registers itself while it runs (``src/utils/tunnel_registry.py``), so
``nodo tunnels`` lists it and ``nodo tunnel_close`` stops it -- from another shell,
a script, or the TUI. ``--detach`` starts the same command in the background and
returns once the listener is bound, which is what the TUI and an agent need: neither
can keep a terminal open for the length of a tunnel. A detached tunnel is also
reopened when the node restarts, until it is closed on purpose.

The listener binds to loopback by default — it is a local entry point to a
remote service, not a new way to expose one.

``--udp`` selects the *local* socket type; the node picks the node-to-service
transport from what the slot declares, so the two must match for the tunnel to
make sense end to end. The relay engine itself lives in
``src/tunneling/tunnel_client.py``, shared with the gateway's own use of it.
"""

import os
import signal
import socket
import sys
import threading
from typing import List, Optional

from src.manager.manager import resolve_instance_token
from src.identity.node_identity import get_node_public_key_hex
from src.tunneling.tunnel_client import (
    DEFAULT_UDP_IDLE_TIMEOUT_S,
    serve_tcp,
    serve_udp,
)
from src.utils import tunnel_registry as registry
from src.utils.config import ConfigManager

env_manager = ConfigManager()

DEFAULT_LISTEN_HOST = "127.0.0.1"

#: How long ``--detach`` waits for the background tunnel to bind and register. The
#: child is a fresh ``nodo`` -- the import graph alone takes seconds on a small host.
DETACH_TIMEOUT_S = 60.0

NODO_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "nodo.py"
)


def _print(message: str) -> None:
    print(message, flush=True)


def _eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def describe(record: dict) -> str:
    """One line for a tunnel: what listens where, and what it reaches."""
    return (
        f"Tunnel {record['id']}: {record['listen_host']}:{record['listen_port']}"
        f"/{record['transport']} -> slot {record['slot']} of {record['token']}"
        f" via {record['gateway']}"
    )


def tunnel(
    instance: str,
    slot: int,
    listen_port: Optional[int] = None,
    listen_host: str = DEFAULT_LISTEN_HOST,
    peer: Optional[str] = None,
    udp: bool = False,
    idle_timeout: float = DEFAULT_UDP_IDLE_TIMEOUT_S,
    as_json: bool = False,
) -> bool:
    """Serve a local port that tunnels to ``slot`` of ``instance``, until stopped.

    ``instance`` may be a local instance name or id when tunnelling through the
    local node; with ``--peer`` it must be the token as the remote node knows it,
    since only that node can resolve it.

    Stops on Ctrl-C or SIGTERM (``nodo tunnel_close``). False when the listener could
    not be bound, which ``nodo.py`` turns into exit status 1.
    """
    from src.commands._catalogue import emit_error, emit_json

    gateway = peer or f"127.0.0.1:{env_manager.get_gateway_port()}"
    # Without --peer the gateway is this node, whose identity we know, so the relay's
    # TLS channel is pinned to it. With --peer the address comes from a person and the
    # id is whatever the certificate proves (issue #257).
    expected_peer_id = None if peer else get_node_public_key_hex()

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
        listener.close()
        return emit_error(as_json, f"Error: cannot bind {listen_host}:{listen_port or 0} -> {e}")

    if not udp:
        listener.listen(16)

    bound_host, bound_port = listener.getsockname()

    # Started by --detach: register under the id the parent is waiting for.
    detached_id = os.environ.get(registry.ID_ENV)
    tunnel_id = detached_id or registry.new_id()
    record = registry.new_record(
        tunnel_id=tunnel_id,
        instance=instance,
        token=token,
        slot=slot,
        udp=udp,
        listen_host=bound_host,
        listen_port=bound_port,
        gateway=gateway,
        peer=peer,
        detached=bool(detached_id),
        log=registry.log_path(tunnel_id) if detached_id else None,
    )
    registered = True
    try:
        registry.register(record)
    except OSError as e:
        # The tunnel works without its file; it just cannot be listed or closed by id.
        registered = False
        _eprint(f"Warning: not registered, so `nodo tunnels` will not list it ({e}).")

    # With --json, stdout carries exactly one object; the per-connection log goes to
    # stderr so it does not follow it.
    log = _eprint if as_json else _print
    if as_json:
        emit_json({"tunnel": record})
    else:
        _print(f"Tunnel {tunnel_id} listening on {bound_host}:{bound_port}/{record['transport']}")
        _print(f"  -> slot {slot} of {token} via {gateway}")
        _print(f"Press Ctrl-C (or run `nodo tunnel_close {tunnel_id}`) to stop.")

    # SIGTERM is how `nodo tunnel_close` asks; the serve loops poll this between
    # accepts, so the listener closes and the file goes with it.
    stop = threading.Event()
    previous_handler = signal.signal(signal.SIGTERM, lambda *_: stop.set())

    try:
        if udp:
            serve_udp(
                listener=listener,
                token=token,
                slot=slot,
                gateway=gateway,
                idle_timeout=idle_timeout,
                log=log,
                should_stop=stop,
                expected_peer_id=expected_peer_id,
            )
        else:
            serve_tcp(
                listener=listener,
                token=token,
                slot=slot,
                gateway=gateway,
                log=log,
                should_stop=stop,
                expected_peer_id=expected_peer_id,
            )
        log("Stopping tunnel.")

    except KeyboardInterrupt:
        log("\nStopping tunnel.")

    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        listener.close()
        if registered:
            registry.unregister(tunnel_id)

    return True


def detach(argv: List[str], as_json: bool = False, timeout_s: float = DETACH_TIMEOUT_S) -> bool:
    """Run ``nodo tunnel <argv>`` in the background; return once it is listening.

    The child is the same command with its output sent to ``<id>.log`` beside its
    registry file (``tunnel_registry.spawn_detached``). Its spec is kept too, pinned
    to the port it got, so the daemon reopens it after a restart
    (``tunnel_registry.restore``) until it is closed on purpose.
    """
    from src.commands._catalogue import emit_error, emit_json

    record, error = registry.spawn_detached(argv, NODO_PY, timeout_s=timeout_s)
    if record is None:
        return emit_error(as_json, error)
    try:
        registry.save_spec(record, argv)
        record["persistent"] = True
    except OSError as e:
        record["persistent"] = False
        _eprint(f"Warning: it will not be reopened after a restart ({e}).")
    if as_json:
        emit_json({"tunnel": record})
    else:
        _print(f"Started in the background (pid {record['pid']}, log {record['log']}).")
        _print("It is reopened when the node restarts, until it is closed.")
        _print(f"Stop it with `nodo tunnel_close {record['id']}`.")
        _print(describe(record))
    return True


def restore_detached() -> int:
    """Reopen the detached tunnels of the last run; called once at daemon start."""
    from src.manager.manager import resolve_instance_token
    from src.utils.logger import LOGGER

    return registry.restore(
        nodo_py=NODO_PY,
        instance_exists=lambda reference: bool(resolve_instance_token(reference=reference)),
        log=LOGGER,
    )
