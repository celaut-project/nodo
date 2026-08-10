"""Service tunneling — reach a local instance's declared slot through the node's own port.

Only the node's gateway port needs to be reachable from the Internet. A caller
that holds an instance's token opens a ``Gateway.ServiceTunnel`` stream and gets
a pipe to one of that instance's declared slots, so no service needs a port of
its own forwarded through NAT.

Wire protocol (beeRPC, ``bee_rpc``)
-----------------------------------
The stream is ``TokenMessage | bytes -> bytes``:

1. The caller sends exactly one ``TokenMessage(token, slot)`` as the first
   message — the handshake. ``token`` is the instance token (the value
   ``StartService`` returns as ``ServiceInstance.token``) and ``slot`` is the
   decimal internal port to reach.
2. Every subsequent message is raw ``bytes``, forwarded to the service.
3. Everything the service sends back is yielded as raw ``bytes``.

beeRPC delivers messages whole: one message in is one message out, never merged
or split. That is what makes datagram slots possible (see *Transports*).

Authorization
-------------
Possession of the instance token is the credential — the same rule
``GetMetrics`` applies. The reachable surface is deliberately narrower than the
instance's network, though: only ports the service *declares* in its API and
that the node actually published as a ``uri_slot`` can be tunnelled to. Without
that check the token would grant access to every port inside the microVM,
including internal ones the service never meant to expose.

Metering
--------
Relaying is metered against the instance's balance: a fixed charge to open
(``pricing.TUNNEL_OPEN_ERG``) and then per byte relayed in either direction
(``pricing.NET_ERG_PER_GIB``), billed every
``costs.TUNNEL_CHARGE_INTERVAL_KB``. Running out closes the tunnel, the way
``maintain`` stops an instance that can no longer pay. See ``TrafficMeter``.

Transports
----------
The node-to-service leg follows the transport the slot declares in
``service.api.slot.transport``; the caller does not choose it.

**TCP.** A byte stream. Message boundaries on this leg are *not* meaningful: one
``recv`` may coalesce or split what the service wrote, exactly like any socket.
When the caller's payload ends, the node half-closes its write side so a
request/response service sees EOF and can still answer.

**UDP.** One datagram per beeRPC message, in both directions — the boundary
preservation above is what carries datagram framing end to end. Three semantic
differences a caller must expect, all inherent to carrying datagrams over a
stream:

* *Reliable and ordered.* The beeRPC leg runs over TCP/HTTP2, so datagrams are
  never lost or reordered between caller and node. Code that relies on UDP being
  lossy will not see loss here — and head-of-line blocking on the beeRPC leg can
  add latency a bare UDP path would not have.
* *No EOF.* A datagram socket never reports the peer closing, so the relay ends
  on RPC cancellation or after ``network.TUNNEL_UDP_IDLE_TIMEOUT_S`` of silence
  once the caller has stopped sending.
* *No connect-time failure.* UDP has no handshake, so an unreachable service
  cannot be detected when the tunnel opens; it surfaces later as an ICMP-driven
  error or as silence.
* *Zero-length datagrams are dropped.* beeRPC cannot represent an empty message
  (the parser cannot tell it from no message at all), so a legal empty datagram
  cannot cross the tunnel. Drops are counted and logged rather than hidden.

QUIC as the *caller-to-node* transport is a separate, future front end; it does
not change anything here.
"""

import select
import socket
import threading
import time
from typing import Callable, Generator, Iterator, List, Optional, Tuple

from protos import celaut_pb2
from src.database.sql_connection import SQLConnection
from src.utils.config import ConfigManager
from src.utils.cost_functions.execution_cost import traffic_charge_mu
from src.utils.logger import LOGGER as logger
from src.utils.monetary import mu_to_erg_str, prices
from src.virtualizers.firewall import TransportProtocol, resolve_slot_transport_protocols

sc = SQLConnection()
env_manager = ConfigManager()

# Read size for the service -> caller direction on TCP. Kept well under bee_rpc's
# 1 MiB CHUNK_SIZE so every chunk travels as a single buffer instead of being
# spilled to a temporary file by the serializer.
RECV_BUFFER_SIZE = 64 * 1024

# A whole datagram must be read in one recv() or the tail is discarded by the
# kernel, so read the largest an IPv4 datagram can be.
MAX_DATAGRAM_SIZE = 65535

# How long to wait for a TCP service to accept the connection.
CONNECT_TIMEOUT_S = 10.0

# The reader blocks in select() at most this long, so a cancelled RPC is noticed
# even while the service is idle.
POLL_INTERVAL_S = 0.5

# Fallback when network.TUNNEL_UDP_IDLE_TIMEOUT_S is unset: how long a UDP relay
# keeps waiting for replies after the caller stopped sending.
DEFAULT_UDP_IDLE_TIMEOUT_S = 30.0

# Grace period for the writer thread to notice the tunnel is closing.
WRITER_JOIN_TIMEOUT_S = 2.0

DEFAULT_TUNNEL_CHARGE_INTERVAL_KB = 1024  # Bill once per MiB relayed.

BYTES_PER_KB = 1024

LOG_PREFIX = "[TUNNEL]"


class TunnelError(Exception):
    """A tunnel could not be established. The gateway maps this to a gRPC status.

    Raised only *before* any payload has been relayed, so the caller either gets
    a clean error status or a working pipe — never a stream that looks empty
    because the handshake quietly failed.
    """


class TrafficMeter:
    """Charges an instance's balance for the traffic its tunnel relays.

    Relaying costs this node real CPU, memory and bandwidth, so it is metered the
    same way ``maintain`` meters a running instance: charged to the instance, and
    when the balance runs out the thing being paid for stops. The instance is the
    right payer because possession of its token is what authorises the tunnel in
    the first place.

    Billing is incremental — every ``costs.TUNNEL_CHARGE_INTERVAL_KB`` of
    traffic, counting both directions — because a tunnel has no fixed length and
    charging only at the end would let a caller relay for free by never closing.
    Whatever is left over is settled when the tunnel closes.

    Traffic is accounted *after* it moves, never before, so data already written
    is never thrown away for lack of funds. The cost is that an empty balance is
    noticed one block late: a tunnel can overrun by up to the charge interval
    before it closes. Shrink the interval to tighten that bound.

    The two rates are independent knobs, each self-disabling at zero:
    ``pricing.TUNNEL_OPEN_ERG`` of 0 makes opening free (``charge_open`` spends 0, which
    ``_spend`` treats as always affordable) and ``pricing.NET_ERG_PER_GIB`` of 0
    disables the per-traffic charge (``enabled`` is False, so ``add``/``settle``
    no-op). Zero on one does not disable the other. Note that whether an empty
    balance actually stops a tunnel depends on ``costs.ALLOW_DEBT``, which
    ``spend_mu`` honours for us.
    """

    def __init__(self, token: str, target: str) -> None:
        self.token = token
        self.target = target
        self.exhausted = threading.Event()

        self._lock = threading.Lock()
        self._unbilled_bytes = 0
        self._billed_mu = 0
        self._relayed_bytes = 0

        try:
            interval_kb = int(
                env_manager.get(
                    "costs.TUNNEL_CHARGE_INTERVAL_KB", DEFAULT_TUNNEL_CHARGE_INTERVAL_KB
                )
            )
        except (TypeError, ValueError):
            interval_kb = DEFAULT_TUNNEL_CHARGE_INTERVAL_KB
        self._interval_bytes = max(1, interval_kb) * BYTES_PER_KB

    @property
    def enabled(self) -> bool:
        return prices().net_mu_per_gib > 0

    def _spend(self, amount_mu: int) -> bool:
        if amount_mu <= 0:
            return True

        # Imported here rather than at module scope: the manager pulls in the
        # virtualizer stack, and the relay must stay importable without it.
        from src.manager.manager import spend_mu

        if spend_mu(id=self.token, amount_mu=amount_mu, debug_mode=False):
            # _spend() itself runs unlocked (spend_mu can be slow, and add()
            # already serializes the accounting that decides the amount), but the
            # writer thread and this generator's thread can both land here, so
            # the increment itself still needs the lock.
            with self._lock:
                self._billed_mu += amount_mu
            return True

        logger(
            f"{LOG_PREFIX} {self.target}: out of funds after {self._relayed_bytes} bytes "
            f"({mu_to_erg_str(self._billed_mu)} ERG billed); closing the tunnel."
        )
        self.exhausted.set()
        return False

    def charge_open(self) -> bool:
        """Charge for opening a tunnel. False means the caller cannot afford it.

        Independent of the traffic rate: a ``pricing.TUNNEL_OPEN_ERG`` of 0 spends
        nothing and always returns True, even when traffic metering is disabled.
        """
        return self._spend(prices().tunnel_open_mu)

    def add(self, byte_count: int) -> bool:
        """Account ``byte_count`` of relayed traffic, billing whole blocks.

        Returns False once the balance is gone, which the relay treats as a close.
        """
        if not self.enabled or byte_count <= 0:
            return not self.exhausted.is_set()

        with self._lock:
            self._relayed_bytes += byte_count
            self._unbilled_bytes += byte_count
            blocks, self._unbilled_bytes = divmod(self._unbilled_bytes, self._interval_bytes)
            if not blocks:
                return not self.exhausted.is_set()
            amount_mu = traffic_charge_mu(blocks * self._interval_bytes)

        return self._spend(amount_mu)

    def settle(self) -> None:
        """Bill the partial block left over when the tunnel closes."""
        if not self.enabled:
            return

        with self._lock:
            pending, self._unbilled_bytes = self._unbilled_bytes, 0

        if pending:
            self._spend(traffic_charge_mu(pending))

        logger(
            f"{LOG_PREFIX} {self.target}: billed {mu_to_erg_str(self._billed_mu)} ERG for "
            f"{self._relayed_bytes} bytes."
        )


def _udp_idle_timeout() -> float:
    """Read the UDP idle timeout at use time, so a config reload takes effect."""
    try:
        configured = env_manager.get("network.TUNNEL_UDP_IDLE_TIMEOUT_S", DEFAULT_UDP_IDLE_TIMEOUT_S)
        timeout = float(configured)
        return timeout if timeout > 0 else DEFAULT_UDP_IDLE_TIMEOUT_S
    except (TypeError, ValueError):
        logger(
            f"{LOG_PREFIX} network.TUNNEL_UDP_IDLE_TIMEOUT_S is not a number; "
            f"using {DEFAULT_UDP_IDLE_TIMEOUT_S}s."
        )
        return DEFAULT_UDP_IDLE_TIMEOUT_S


def _handshake(iterator: Iterator) -> Tuple[str, str]:
    """Consume the leading ``TokenMessage`` and return ``(token, slot)``."""
    first = next(iterator, None)

    if first is None:
        raise TunnelError("Empty tunnel stream: a TokenMessage must be sent first.")

    if not isinstance(first, celaut_pb2.TokenMessage):
        raise TunnelError(
            f"The first tunnel message must be a TokenMessage, got {type(first).__name__}."
        )

    if not first.token:
        raise TunnelError("TokenMessage carries no token.")

    if not first.HasField("slot") or not first.slot:
        raise TunnelError("TokenMessage carries no slot; the target port is required.")

    return first.token, first.slot


def _declared_slot_ports(instance: celaut_pb2.Instance) -> set:
    """Internal ports the node published for this instance."""
    return {uri_slot.internal_port for uri_slot in instance.uri_slot}


def _slot_transport(
    instance: celaut_pb2.Instance, port: int
) -> Optional[TransportProtocol]:
    """Transport declared for ``port`` in the instance's API, if it resolves."""
    for api_slot in instance.api.slot:
        if api_slot.port != port:
            continue
        try:
            return resolve_slot_transport_protocols(
                api_slot,
                logger_fn=logger,
                context=f"{LOG_PREFIX}",
            )
        except ValueError as e:
            logger(f"{LOG_PREFIX} Slot {port} has an unusable transport definition: {e}")
            return None
    return None


def _resolve_target(token: str, slot: str) -> Tuple[str, int, TransportProtocol]:
    """Map ``(token, slot)`` to the address and transport of a tunnellable slot.

    Raises ``TunnelError`` unless the instance is known and the slot is one the
    instance declared with a host-supported transport.
    """
    try:
        port = int(slot)
    except ValueError:
        raise TunnelError(f"Slot '{slot}' is not a port number.")

    serialized_instance = sc.get_internal_instance(id=token)
    if not serialized_instance:
        raise TunnelError(
            f"No local instance for token '{token}'. Tunnels only reach instances "
            "running on this node; a delegated token must be tunnelled through the "
            "node that runs it."
        )

    instance = celaut_pb2.Instance()
    try:
        instance.ParseFromString(serialized_instance)
    except Exception as e:
        raise TunnelError(f"Stored instance for token '{token}' is unreadable: {e}")

    declared = _declared_slot_ports(instance)
    if port not in declared:
        raise TunnelError(
            f"Slot {port} is not declared by instance '{token}'. "
            f"Declared slots: {sorted(declared) if declared else 'none'}."
        )

    transport = _slot_transport(instance, port)
    if transport is None:
        raise TunnelError(
            f"Slot {port} of instance '{token}' declares no host-supported transport."
        )

    ip = sc.get_internal_ip(id=token)
    if not ip:
        raise TunnelError(f"Instance '{token}' has no internal IP recorded.")

    return ip, port, transport


def _connect(ip: str, port: int, transport: TransportProtocol) -> socket.socket:
    """Open the node -> service leg using the transport the slot declared."""
    if transport is TransportProtocol.UDP:
        conn = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Connected UDP: lets us use send/recv and surfaces ICMP errors
            # (port unreachable) on the socket instead of losing them.
            conn.connect((ip, port))
        except OSError as e:
            conn.close()
            raise TunnelError(f"Cannot bind a UDP socket towards {ip}:{port}: {e}")
        return conn

    try:
        conn = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT_S)
    except OSError as e:
        raise TunnelError(f"Cannot reach the service at {ip}:{port}: {e}")

    # Back to blocking mode: the writer thread relies on sendall() completing, and
    # a socket timeout would make sendall() raise without reporting how much of
    # the payload it already wrote — silently corrupting the stream. The reader
    # gets its own cancellation check from select() instead.
    conn.settimeout(None)
    return conn


def _pump_to_service(
    iterator: Iterator,
    conn: socket.socket,
    stop: threading.Event,
    caller_done: threading.Event,
    target: str,
    is_udp: bool,
    meter: TrafficMeter,
    activity: List[float],
) -> None:
    """Forward caller payload to the service until the caller stops sending.

    Runs in its own thread: gRPC hands us a *blocking* request iterator and
    consumes our response generator on this handler's thread, so the two
    directions cannot be driven by one loop without deadlocking one of them.
    """
    sent = 0
    messages = 0
    try:
        for message in iterator:
            if stop.is_set():
                break

            if not isinstance(message, bytes):
                logger(
                    f"{LOG_PREFIX} {target}: ignoring unexpected "
                    f"{type(message).__name__} in the payload stream."
                )
                continue

            if is_udp:
                try:
                    # One message is one datagram; send() never splits it.
                    conn.send(message)
                except OSError as e:
                    # An oversized datagram (EMSGSIZE) or a transient ICMP error
                    # is this message's problem, not the tunnel's.
                    logger(f"{LOG_PREFIX} {target}: dropped a {len(message)}-byte datagram: {e}")
                    continue
            else:
                conn.sendall(message)

            sent += len(message)
            messages += 1
            activity[0] = time.monotonic()  # caller->service counts as activity too

            if not meter.add(len(message)):
                break  # Out of funds.

    except Exception as e:
        # Includes the caller cancelling the RPC and the service closing its
        # read side; neither is worth a traceback.
        if not stop.is_set():
            logger(f"{LOG_PREFIX} {target}: caller -> service stopped: {e}")

    finally:
        logger(
            f"{LOG_PREFIX} {target}: caller -> service closed after {sent} bytes "
            f"in {messages} messages."
        )
        caller_done.set()
        if not is_udp:
            # Half-close so a request/response service sees EOF and can answer
            # before we tear the socket down. UDP has no such thing.
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass


def _relay(
    iterator: Iterator,
    conn: socket.socket,
    is_active: Callable[[], bool],
    target: str,
    is_udp: bool,
    meter: TrafficMeter,
) -> Generator[bytes, None, None]:
    """Yield everything the service sends while forwarding the caller's payload."""
    stop = threading.Event()
    caller_done = threading.Event()
    # Shared last-activity clock (a one-element list so both threads see writes),
    # bumped on traffic in either direction; the UDP idle timeout reads it.
    activity = [time.monotonic()]
    writer = threading.Thread(
        target=_pump_to_service,
        kwargs={
            "iterator": iterator,
            "conn": conn,
            "stop": stop,
            "caller_done": caller_done,
            "target": target,
            "is_udp": is_udp,
            "meter": meter,
            "activity": activity,
        },
        name=f"tunnel-writer-{target}",
        daemon=True,
    )
    writer.start()

    read_size = MAX_DATAGRAM_SIZE if is_udp else RECV_BUFFER_SIZE
    idle_timeout = _udp_idle_timeout() if is_udp else None
    received = 0
    messages = 0
    empty_datagrams = 0

    try:
        while is_active() and not meter.exhausted.is_set():
            readable, _, _ = select.select([conn], [], [], POLL_INTERVAL_S)

            if not readable:
                # A datagram socket never reports EOF, so silence is the only
                # signal we get that an exchange is over. Fire on inactivity in
                # *either* direction, not only after the caller half-closes: a
                # UDP caller that never ends its request stream would otherwise
                # hold the relay, socket and billing loop open indefinitely.
                if is_udp and (time.monotonic() - activity[0] > idle_timeout):
                    logger(f"{LOG_PREFIX} {target}: idle for {idle_timeout}s, closing.")
                    break
                continue

            data = conn.recv(read_size)
            activity[0] = time.monotonic()

            if not data:
                if not is_udp:  # The TCP service closed its write side.
                    break
                # A zero-length datagram is legal on the wire but cannot cross
                # beeRPC: an empty message is indistinguishable from no message,
                # and the parser drops it. Count it so the loss is visible.
                empty_datagrams += 1
                continue

            received += len(data)
            messages += 1

            # Charged before handing it over: what the caller receives is what it
            # pays for, and an exhausted balance stops the next read, not this one.
            billable = meter.add(len(data))
            yield data
            if not billable:
                break  # Out of funds.

    except OSError as e:
        # For connected UDP this is where an ICMP port-unreachable lands.
        logger(f"{LOG_PREFIX} {target}: service -> caller stopped: {e}")

    finally:
        logger(
            f"{LOG_PREFIX} {target}: service -> caller closed after {received} bytes "
            f"in {messages} messages."
            + (f" Dropped {empty_datagrams} zero-length datagrams." if empty_datagrams else "")
        )
        stop.set()
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()
        # The writer may still be parked on the caller's iterator; it is a daemon
        # and gRPC breaks that iterator when the RPC ends, so don't wait forever.
        writer.join(timeout=WRITER_JOIN_TIMEOUT_S)
        meter.settle()


def service_tunnel(
    iterator: Iterator,
    is_active: Callable[[], bool] = lambda: True,
) -> Tuple[socket.socket, Generator[bytes, None, None]]:
    """Establish a tunnel and return ``(conn, relay)``.

    Deliberately *not* a generator itself: the handshake, slot validation and
    connect all run eagerly on call, so a ``TunnelError`` surfaces before the
    gateway has serialized a single buffer and can still become a gRPC status.

    The connected socket is returned alongside the relay generator because it is
    opened eagerly, before the generator is iterated. ``_relay``'s own ``finally``
    closes it, but that only runs once the generator has been entered; if the
    consumer (``serialize_to_buffer``) fails before its first pull, the socket
    would leak. Handing ``conn`` back lets the caller close it unconditionally.

    ``is_active`` is polled while relaying (the gateway passes
    ``context.is_active``) so a cancelled RPC tears the tunnel down instead of
    leaving it parked on an idle service.
    """
    token, slot = _handshake(iterator)
    logger(f"{LOG_PREFIX} Handshake for token={token} slot={slot}")

    ip, port, transport = _resolve_target(token, slot)
    target = f"{token}@{ip}:{port}/{transport.value}"

    # Charged before connecting: an instance that cannot pay to open the tunnel
    # gets a clean refusal instead of a socket it will lose mid-transfer.
    meter = TrafficMeter(token=token, target=target)
    if not meter.charge_open():
        raise TunnelError(
            f"Instance '{token}' has not enough balance to open a tunnel."
        )

    conn = _connect(ip, port, transport)
    logger(f"{LOG_PREFIX} {target}: tunnel established.")

    relay = _relay(
        iterator=iterator,
        conn=conn,
        is_active=is_active,
        target=target,
        is_udp=transport is TransportProtocol.UDP,
        meter=meter,
    )
    return conn, relay
