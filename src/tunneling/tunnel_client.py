"""Caller side of service tunneling — turn a local socket into a ServiceTunnel stream.

The relay engine, with no CLI in it, because it has two consumers:

* ``nodo tunnel`` (``src/commands/tunnel.py``), where a person wants a local port;
* the gateway itself (``src/tunneling/delegated_endpoints.py``), where a delegated
  instance's ``uri_slot`` is rewritten to a local endpoint so our own client can
  reach a service running on a peer it cannot address directly.

Both need the same thing: a local listener whose traffic is carried over
``Gateway.ServiceTunnel``. They differ only in where messages get logged and in
who decides when to stop, so both are injected.

TCP is connection-per-stream. UDP has no connections, so a flow is synthesised
per source address and reaped when it goes quiet — see ``serve_udp``.
"""

import queue
import socket
import threading
import time
from typing import Callable, Dict, Generator, Optional, Tuple

import grpc

from bee_rpc.client import client_grpc
from protos import celaut_pb2, celaut_pb2_grpc

# Read size for the local socket -> node direction, matching the relay's own
# buffer and staying well under bee_rpc's 1 MiB chunk threshold.
RECV_BUFFER_SIZE = 64 * 1024

# A datagram must be read whole or the kernel discards its tail.
MAX_DATAGRAM_SIZE = 65535

# How long a UDP flow with no traffic is kept before its stream is closed.
DEFAULT_UDP_IDLE_TIMEOUT_S = 30.0

# How often a blocked listener or flow re-checks whether it should stop.
POLL_INTERVAL_S = 0.5


def open_stream(
    gateway: str,
    token: str,
    slot: int,
    outbound: Generator,
    channel: Optional[grpc.Channel] = None,
):
    """Open a ServiceTunnel stream: handshake first, then ``outbound``'s payload."""

    def with_handshake() -> Generator:
        yield celaut_pb2.TokenMessage(token=token, slot=str(slot))
        yield from outbound

    stub = celaut_pb2_grpc.GatewayStub(channel or grpc.insecure_channel(gateway))
    return client_grpc(
        method=stub.ServiceTunnel,
        input=with_handshake(),
        indices_parser={0: bytes},
        partitions_message_mode_parser=True,
        indices_serializer={1: celaut_pb2.TokenMessage},
    )


def bridge_tcp_connection(
    sock: socket.socket,
    token: str,
    slot: int,
    gateway: str,
    label: str,
    log: Callable[[str], None],
) -> None:
    """Bridge one accepted TCP connection to a stream until either end closes."""
    closing = threading.Event()

    def outbound() -> Generator:
        try:
            while not closing.is_set():
                data = sock.recv(RECV_BUFFER_SIZE)
                if not data:  # The local client closed its write side.
                    break
                yield data
        except OSError:
            pass  # Socket torn down while we were reading; the stream ends here.

    channel = grpc.insecure_channel(gateway)
    try:
        for chunk in open_stream(gateway, token, slot, outbound(), channel=channel):
            if isinstance(chunk, bytes):
                sock.sendall(chunk)

    except grpc.RpcError as e:
        log(f"[{label}] tunnel error: {e.details() or e}")
    except OSError as e:
        log(f"[{label}] connection closed: {e}")

    finally:
        closing.set()
        # Unblock outbound()'s recv() and let the local client see EOF.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        channel.close()
        log(f"[{label}] closed")


class UdpFlow:
    """One source address's stream: a queue in, datagrams back to that source.

    UDP gives us no connection to hang a stream off, so a flow is synthesised per
    source address and reaped once it goes quiet.
    """

    def __init__(
        self,
        listener: socket.socket,
        source: Tuple[str, int],
        token: str,
        slot: int,
        gateway: str,
        idle_timeout: float,
        log: Callable[[str], None],
    ) -> None:
        self.listener = listener
        self.source = source
        self.label = f"{source[0]}:{source[1]}"
        self.outbox: queue.Queue = queue.Queue()
        self.closed = threading.Event()
        self.last_seen = time.monotonic()
        self._token = token
        self._slot = slot
        self._gateway = gateway
        self._idle_timeout = idle_timeout
        self._log = log
        self._thread = threading.Thread(
            target=self._run, name=f"tunnel-udp-{self.label}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def submit(self, datagram: bytes) -> None:
        self.last_seen = time.monotonic()
        self.outbox.put(datagram)

    def is_idle(self, now: float) -> bool:
        return now - self.last_seen > self._idle_timeout

    def close(self) -> None:
        self.closed.set()

    def _outbound(self) -> Generator:
        while not self.closed.is_set():
            try:
                yield self.outbox.get(timeout=POLL_INTERVAL_S)
            except queue.Empty:
                if self.is_idle(time.monotonic()):
                    return  # Ends the stream; the node's relay closes its side.

    def _run(self) -> None:
        channel = grpc.insecure_channel(self._gateway)
        try:
            for chunk in open_stream(
                self._gateway, self._token, self._slot, self._outbound(), channel=channel
            ):
                if isinstance(chunk, bytes):
                    # One beeRPC message back out as one datagram.
                    self.listener.sendto(chunk, self.source)
                    self.last_seen = time.monotonic()

        except grpc.RpcError as e:
            self._log(f"[{self.label}] tunnel error: {e.details() or e}")
        except OSError as e:
            self._log(f"[{self.label}] flow closed: {e}")

        finally:
            self.closed.set()
            channel.close()
            self._log(f"[{self.label}] flow closed")


def serve_udp(
    listener: socket.socket,
    token: str,
    slot: int,
    gateway: str,
    idle_timeout: float = DEFAULT_UDP_IDLE_TIMEOUT_S,
    log: Callable[[str], None] = lambda message: None,
    should_stop: Optional[threading.Event] = None,
) -> None:
    """Route datagrams to a per-source flow, reaping flows that go quiet."""
    flows: Dict[Tuple[str, int], UdpFlow] = {}
    listener.settimeout(POLL_INTERVAL_S)

    while should_stop is None or not should_stop.is_set():
        try:
            datagram, source = listener.recvfrom(MAX_DATAGRAM_SIZE)
        except socket.timeout:
            datagram, source = None, None
        except OSError:
            break  # Listener closed.

        now = time.monotonic()
        for key, flow in list(flows.items()):
            if flow.closed.is_set() or flow.is_idle(now):
                flow.close()
                del flows[key]

        if datagram is None:
            continue

        flow = flows.get(source)
        if flow is None:
            flow = UdpFlow(
                listener=listener,
                source=source,
                token=token,
                slot=slot,
                gateway=gateway,
                idle_timeout=idle_timeout,
                log=log,
            )
            flows[source] = flow
            log(f"[{flow.label}] flow opened")
            flow.start()

        flow.submit(datagram)

    for flow in flows.values():
        flow.close()


def serve_tcp(
    listener: socket.socket,
    token: str,
    slot: int,
    gateway: str,
    log: Callable[[str], None] = lambda message: None,
    should_stop: Optional[threading.Event] = None,
) -> None:
    """One accepted connection, one stream."""
    listener.settimeout(POLL_INTERVAL_S)

    while should_stop is None or not should_stop.is_set():
        try:
            sock, addr = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            break  # Listener closed.

        label = f"{addr[0]}:{addr[1]}"
        log(f"[{label}] connected")
        threading.Thread(
            target=bridge_tcp_connection,
            kwargs={
                "sock": sock,
                "token": token,
                "slot": slot,
                "gateway": gateway,
                "label": label,
                "log": log,
            },
            name=f"tunnel-{label}",
            daemon=True,
        ).start()
