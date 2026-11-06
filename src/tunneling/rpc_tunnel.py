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
4. If the ``TokenMessage`` has ``receipts``, the node also yields signed
   ``ExecutionReceipt`` checkpoints at index 2, and a ``close`` one when the stream
   stops normally. See *Receipts*.

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
(``pricing.TUNNEL_OPEN_MU``) and then per byte relayed in either direction
(``pricing.NET_MU_PER_GIB``), billed every
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

Receipts
--------
A caller that asks for them (``TokenMessage.receipts``) gets checkpoints of the
execution receipt (#419, docs/EXECUTION_RECEIPTS.md): after each successful
``TrafficMeter.add``, the relayed message goes into one of two hash chains (into
the service, out of it), and every ``receipts.CHECKPOINT_BYTES`` or
``receipts.CHECKPOINT_SECONDS`` the node signs the counters, the two heads and what
the tunnel was charged. A message that the meter refuses (no balance, no daily
allowance) is not in the chains, so the signed counters show only accepted traffic. Every
``tunnel`` movement of the ledger names the execution (``execution_name``) and, with
receipts, the stream (``session``), so the charge of a stream in the balance statement
is the ``mu_charged`` of its close (after a cancel, at least that of its last checkpoint). A caller that does not ask gets no index 2,
and nothing is hashed for it.

Descendants (#631)
------------------
A caller that holds the token of an ancestor sends a ``DescendantTunnel`` at index 2
in place of the ``TokenMessage``: the token, the ``instance_ref`` of a descendant, the
slot, ``receipts`` and ``remaining_hops``. The node finds the descendant through the
``child_bound`` rows, as the read of its statement does
(``balance_statement.locate``). The token of the descendant never leaves the node.

* A local descendant that is alive: the node opens the tunnel as for its own token.
  The descendant pays (the orphan pays its traffic).
* A descendant on a peer: the node relays the bytes to the peer with a
  ``DescendantTunnel`` of its own, with the token that the peer gave it and one hop
  less. Each node meters its own leg. The leg is charged to the descendant when this
  node holds its account (the alias of a delegated descendant), else only the
  operator's ceilings apply. A node does not forward the receipts of the next node:
  they name that node. It signs its own leg when the caller asked for receipts.
* Receipts of this kind of stream name the execution by the ``instance_ref``
  (``execution_receipt.execution_name_by_ref``).
* A hop that fails with the descendant unavailable is written down for the peer and
  the ``instance_ref`` (``descendant_tunnels``), and so is a hop that relays a byte.
  A statement of the descendant that still charges maintenance after the failure is
  a fault of the peer (``balance_statement._judge_unreachable``).

No error that goes to the caller names a token, a peer or an address: the caller can
be a service, and a service does not know nodes.
"""

import contextlib
import select
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Generator, Iterator, List, Optional, Tuple

from protos import celaut_pb2
from src.database.sql_connection import SQLConnection
from src.identity import execution_receipt
from src.tunneling import inbound, logger
from src.utils import host_limits
from src.utils.config import ConfigManager
from src.utils.cost_functions.execution_cost import traffic_charge_mu
from src.utils.monetary import format_mu, prices
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

# How long a tunnel with receipts waits, at its end, for a message that the writer thread
# is still accounting (a slow ledger write, or the shaping wait) before it signs the
# close. It waits only while the writer is inside that step, so a normal close is not
# slower. The wait has an end: a ledger that does not answer must not hold the stream
# open (#419).
CLOSE_CHARGE_WAIT_S = 10.0

# Bill once per 64 MiB relayed. Every charge is a ledger transaction that waits for
# the disk, and the relay does not read the service socket while it waits, so a
# small interval caps throughput at (interval / commit time): 1 MiB per charge ran
# at 5 MiB/s on a spinning disk, 64 MiB at 47 MiB/s. See docs/TUNNELING.md.
DEFAULT_TUNNEL_CHARGE_INTERVAL_KB = 65536

BYTES_PER_KB = 1024

LOG_PREFIX = "[TUNNEL]"


class TunnelError(Exception):
    """A tunnel could not be established. The gateway maps this to a gRPC status.

    Raised only *before* any payload has been relayed, so the caller either gets
    a clean error status or a working pipe — never a stream that looks empty
    because the handshake quietly failed. The one exception is the relay to a
    peer (``_relay_peer``): the refusal of the next node comes only when the
    stream runs, and the relay raises it then, with the same classes.

    The text goes to the caller. It names no token, no peer and no address.
    The base class is ``INVALID_ARGUMENT``.
    """


class TunnelRefused(TunnelError):
    """No chain below the token links the descendant. ``PERMISSION_DENIED``."""


class TunnelExhausted(TunnelError):
    """The instance cannot pay, the node has no traffic allowance left, or the hop
    budget is used up. ``RESOURCE_EXHAUSTED``.

    A node above does not write this refusal as a failure of the hop: the caller sets
    the hop budget, and a small budget is not a fault of the peer."""


class TunnelUnavailable(TunnelError):
    """The instance does not answer, is not alive, or a node below failed.
    ``UNAVAILABLE``."""


# The texts of a refusal of a descendant tunnel. They name no peer and no token.
_NOT_LINKED = "No instance that this token may reach has that descendant."
_NOT_ALIVE = "The descendant is not alive."
_BELOW_FAILED = "A node below did not open the tunnel."
_BELOW_REFUSED = "A node below refused the tunnel."
_NO_BUDGET = "The hop budget of this tunnel is used up."
_NO_BALANCE = "The instance has not enough balance to open a tunnel."
_READ_FIRST = ("More than one branch can hold that descendant. Read the statement of the "
               "descendant first.")
_MANY_BELOW = "More than one node below gave a statement of that descendant."


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

    Beside the billing, and independent of it, sit the operator's own network ceilings
    (`host_limits`): the day's relayed volume and the throughput across every tunnel at
    once. They are a policy about this machine's connection rather than a price, so
    they are applied whatever the rates say -- a node giving traffic away for free still
    has a daily allowance, and a client with a full balance still gets shaped.

    The two rates are independent knobs, each self-disabling at zero:
    ``pricing.TUNNEL_OPEN_MU`` of 0 makes opening free (``charge_open`` spends 0, which
    ``_spend`` treats as always affordable) and ``pricing.NET_MU_PER_GIB`` of 0
    disables the per-traffic charge (``enabled`` is False, so ``add``/``settle``
    no-op). Zero on one does not disable the other. Note that whether an empty
    balance actually stops a tunnel depends on ``costs.ALLOW_DEBT``, which
    ``spend_mu`` honours for us.
    """

    def __init__(self, token: Optional[str], target: str, cause: Optional[dict] = None,
                 delegated: bool = False) -> None:
        # The payer. None for a relay leg of a descendant whose account is on another
        # node: then only the operator's ceilings apply, and nothing is charged here.
        # ``delegated``: the payer is the alias of an instance that this node delegated
        # (#631). ``spend_mu`` takes only clients and local instances, so the meter
        # writes the row itself.
        self.token = token
        self.delegated = delegated
        self.target = target
        # Added to the detail of each `tunnel` movement: the execution name, and the
        # receipt stream when there is one (#419).
        self.cause = dict(cause or {})
        self.exhausted = threading.Event()

        self._lock = threading.Lock()
        # Wakes `wait_idle` when a message is accounted. It shares `_lock`.
        self._idle = threading.Condition(self._lock)
        self._accounting = 0   # messages that are between their charge and their record
        self._unbilled_bytes = 0
        self._billed_mu = 0
        self._relayed_bytes = 0
        self._open_mu = 0   # the open charge, while ``refund_open`` can give it back

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

    @property
    def billed_mu(self) -> int:
        """What this tunnel has charged so far: the open charge and the billed blocks."""
        with self._lock:
            return self._billed_mu

    def _charge(self, amount_mu: int, detail: dict) -> bool:
        """Take ``amount_mu`` from the payer, with a ``tunnel`` row. True if taken."""
        # Imported here rather than at module scope: the manager pulls in the
        # virtualizer stack, and the relay must stay importable without it.
        from src.database import ledger
        from src.manager.manager import ALLOW_DEBT, spend_mu

        if not self.delegated:
            return spend_mu(id=self.token, amount_mu=amount_mu, debug_mode=False,
                            kind=ledger.TUNNEL, detail={**self.cause, **detail})
        try:
            sc.move_balance(self.token, -int(amount_mu), ledger.TUNNEL,
                            allow_debt=ALLOW_DEBT, detail={**self.cause, **detail})
            return True
        except (ledger.NoSuchAccount, ledger.InsufficientBalance) as e:
            logger(f"{LOG_PREFIX} {self.target}: no charge: {type(e).__name__}.")
            return False

    def _spend(self, amount_mu: int, detail: dict) -> bool:
        if amount_mu <= 0 or not self.token:
            return True

        if self._charge(amount_mu, detail):
            # _spend() itself runs unlocked (spend_mu can be slow, and add()
            # already serializes the accounting that decides the amount), but the
            # writer thread and this generator's thread can both land here, so
            # the increment itself still needs the lock.
            with self._lock:
                self._billed_mu += amount_mu
            return True

        logger(
            f"{LOG_PREFIX} {self.target}: out of funds after {self._relayed_bytes} bytes "
            f"({format_mu(self._billed_mu)} billed); closing the tunnel."
        )
        self.exhausted.set()
        return False

    def charge_open(self) -> bool:
        """Charge for opening a tunnel. False means the caller cannot afford it.

        Independent of the traffic rate: a ``pricing.TUNNEL_OPEN_MU`` of 0 spends
        nothing and always returns True, even when traffic metering is disabled.
        """
        amount_mu = prices().tunnel_open_mu
        if not self._spend(amount_mu, {"open": True}):
            return False
        if self.token and amount_mu > 0:
            with self._lock:
                self._open_mu = int(amount_mu)
        return True

    def refund_open(self) -> None:
        """Give back the open charge of a relay leg that the next node refused (#631).

        Only for a delegated payer (``delegated``): the leg of a ``DescendantTunnel`` to
        a peer. The node charges the open before the next node checks the tunnel, and
        a node that refuses relays nothing. The refund is a positive ``tunnel`` row with
        ``detail.open_refund``. It is given once.
        """
        from src.database import ledger

        with self._lock:
            amount_mu, self._open_mu = self._open_mu, 0
        if not amount_mu or not self.delegated or not self.token:
            return
        try:
            sc.move_balance(self.token, amount_mu, ledger.TUNNEL,
                            detail={**self.cause, "open_refund": True})
        except Exception as e:
            logger(f"{LOG_PREFIX} {self.target}: the open charge is not given back: "
                   f"{type(e).__name__}.")
            return
        with self._lock:
            self._billed_mu -= amount_mu

    def add(self, byte_count: int) -> bool:
        """Account ``byte_count`` of relayed traffic, billing whole blocks.

        Returns False once the balance or the day's network allowance is gone, either of
        which the relay treats as a close.
        """
        if byte_count <= 0:
            return not self.exhausted.is_set()

        # The operator's ceilings first, and outside the `self.enabled` guard below:
        # `pricing.NET_MU_PER_GIB` of 0 turns off the *charge*, never the ceiling.
        # Shaping before counting so the wait is paid on the bytes that caused it.
        host_limits.throttle_tunnel_traffic(byte_count)
        if not host_limits.account_tunnel_traffic(byte_count):
            logger(
                f"{LOG_PREFIX} {self.target}: {host_limits.daily_allowance_reason()} "
                "Closing the tunnel."
            )
            self.exhausted.set()
            return False

        if not self.enabled:
            return not self.exhausted.is_set()

        # A message that the meter refuses is not in the receipt chain (#419), so none of
        # its bytes are billed: not after the other direction spent the balance, and not
        # the part of it that is left after a refused block. Otherwise the settle bills
        # bytes that no receipt shows, and mu_charged is more than the chains cover.
        with self._lock:
            if self.exhausted.is_set():
                return False
            self._relayed_bytes += byte_count
            self._unbilled_bytes += byte_count
            blocks, self._unbilled_bytes = divmod(self._unbilled_bytes, self._interval_bytes)
            if not blocks:
                return True
            billed_bytes = blocks * self._interval_bytes
            amount_mu = traffic_charge_mu(billed_bytes)
            # When blocks > 0, what is left is part of this message only.
            left_of_this_message = self._unbilled_bytes

        if self._spend(amount_mu, {"bytes": billed_bytes}):
            return True
        with self._lock:
            self._unbilled_bytes -= min(left_of_this_message, self._unbilled_bytes)
        return False

    @contextlib.contextmanager
    def accounting(self) -> Iterator[None]:
        """Mark a message as being accounted: from its charge to its record in the chain.

        The writer thread wraps ``add`` and ``record`` in it. :meth:`wait_idle` waits
        for the mark, so the close does not show a charge without its message in the
        chains, or a message without its charge (#419).
        """
        with self._lock:
            self._accounting += 1
        try:
            yield
        finally:
            with self._idle:
                self._accounting -= 1
                self._idle.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no message is being accounted, at most ``timeout`` seconds.

        Returns False if one still is. The close of a stream with receipts is signed
        after this, so its ``mu_charged`` and its chains cover the same messages (#419).
        """
        with self._idle:
            return self._idle.wait_for(lambda: self._accounting == 0, timeout)

    def settle(self) -> None:
        """Bill the partial block left over when the tunnel closes."""
        # Ahead of the `enabled` guard, and of the billing: the day's total has to reach
        # the database even for a node that charges nothing for traffic, or a run of
        # short tunnels would each leave less than a flush block behind and the
        # allowance would never appear to be spent.
        host_limits.flush_tunnel_traffic()

        if not self.enabled:
            return

        with self._lock:
            pending, self._unbilled_bytes = self._unbilled_bytes, 0

        if pending:
            self._spend(traffic_charge_mu(pending), {"bytes": pending})

        logger(
            f"{LOG_PREFIX} {self.target}: billed {format_mu(self._billed_mu)} for "
            f"{self._relayed_bytes} bytes."
        )


def _udp_idle_timeout() -> float:
    """Read the UDP idle timeout at use time rather than binding it at import."""
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


@dataclass(frozen=True)
class Handshake:
    """The first message of a tunnel.

    ``instance_ref`` is set for a ``DescendantTunnel``: then ``token`` is the token of
    the ancestor, and ``hops`` the ``remaining_hops`` that came (None if absent).
    """
    token: str
    slot: str
    receipts: bool
    instance_ref: str = ""
    hops: Optional[int] = None


def _handshake(iterator: Iterator) -> Handshake:
    """Consume the leading ``TokenMessage`` or ``DescendantTunnel`` and return it."""
    first = next(iterator, None)

    if first is None:
        raise TunnelError("Empty tunnel stream: a TokenMessage must be sent first.")

    if isinstance(first, celaut_pb2.DescendantTunnel):
        if not first.token:
            raise TunnelError("DescendantTunnel carries no token.")
        if not first.instance_ref:
            raise TunnelError("DescendantTunnel carries no instance_ref.")
        if not first.slot:
            raise TunnelError("DescendantTunnel carries no slot; the target port is required.")
        return Handshake(
            token=first.token, slot=first.slot, receipts=bool(first.receipts),
            instance_ref=first.instance_ref,
            hops=first.remaining_hops if first.HasField("remaining_hops") else None,
        )

    if not isinstance(first, celaut_pb2.TokenMessage):
        raise TunnelError(
            f"The first tunnel message must be a TokenMessage, got {type(first).__name__}."
        )

    if not first.token:
        raise TunnelError("TokenMessage carries no token.")

    if not first.HasField("slot") or not first.slot:
        raise TunnelError("TokenMessage carries no slot; the target port is required.")

    return Handshake(token=first.token, slot=first.slot, receipts=bool(first.receipts))


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

    # No text below names the token: the caller can be a service, and an error is no
    # place for a credential.
    serialized_instance = sc.get_internal_instance(id=token)
    if not serialized_instance:
        raise TunnelError(
            "No local instance for this token. Tunnels only reach instances "
            "running on this node; a delegated token must be tunnelled through the "
            "node that runs it, or with a DescendantTunnel from an ancestor."
        )

    instance = celaut_pb2.Instance()
    try:
        instance.ParseFromString(serialized_instance)
    except Exception as e:
        logger(f"{LOG_PREFIX} Stored instance {_ref_label(token)} is unreadable: {e}")
        raise TunnelError("The stored instance is unreadable.")

    declared = _declared_slot_ports(instance)
    if port not in declared:
        raise TunnelError(
            f"Slot {port} is not declared by the instance. "
            f"Declared slots: {sorted(declared) if declared else 'none'}."
        )

    transport = _slot_transport(instance, port)
    if transport is None:
        raise TunnelError(
            f"Slot {port} of the instance declares no host-supported transport."
        )

    ip = sc.get_internal_ip(id=token)
    if not ip:
        raise TunnelError("The instance has no internal IP recorded.")

    return ip, port, transport


def _ref_label(token: str) -> str:
    """A short name of an instance for the log: the start of its ``instance_ref``."""
    from src.database import ledger
    return ledger.instance_ref(token)[:12]


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
            logger(f"{LOG_PREFIX} Cannot bind a UDP socket towards {ip}:{port}: {e}")
            raise TunnelUnavailable(f"Cannot bind a UDP socket towards slot {port}.")
        return conn

    try:
        conn = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT_S)
    except OSError as e:
        # The internal address stays in the log of the node.
        logger(f"{LOG_PREFIX} Cannot reach the service at {ip}:{port}: {e}")
        raise TunnelUnavailable(f"Cannot reach the service at slot {port}: {e.strerror or e}")

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
    stream_id: Optional[str] = None,
    receipts: Optional[execution_receipt.ReceiptChain] = None,
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
            inbound.count(stream_id, bytes_in=len(message))
            activity[0] = time.monotonic()  # caller->service counts as activity too

            # One step for the meter and the chain: the close waits for it (#419).
            with meter.accounting():
                if not meter.add(len(message)):
                    break  # Out of funds. Not billed, so not in the receipt chain.
                if receipts is not None:
                    receipts.record(execution_receipt.DIRECTION_IN, message)

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
    stream: Optional[dict] = None,
    receipts: Optional[execution_receipt.ReceiptChain] = None,
) -> Generator[bytes, None, None]:
    """Yield everything the service sends while forwarding the caller's payload.

    ``stream`` describes the tunnel for the operator's list of inbound streams
    (``inbound.opened``): it is registered once relaying starts and dropped when it
    ends, so a relay that is never iterated is never listed.

    With ``receipts``, it also yields the checkpoints (``ExecutionReceipt``) when they
    are due, and the ``close`` after the tunnel is torn down and settled, so the close
    carries the whole charge. A relay that is closed from outside (the caller cancelled
    the RPC, or the gateway called ``relay.close()``) yields no close: a generator
    cannot yield after ``GeneratorExit``, and nobody is left to receive it. So a
    missing close is not proof of a fault (docs/EXECUTION_RECEIPTS.md).
    """
    stream_id = inbound.opened(**stream) if stream else None
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
            "stream_id": stream_id,
            "receipts": receipts,
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
                if receipts is not None and receipts.due():
                    checkpoint = receipts.checkpoint(mu_charged=meter.billed_mu)
                    if checkpoint is not None:
                        yield checkpoint
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
            inbound.count(stream_id, bytes_out=len(data))

            # Charged before handing it over: what the caller receives is what it
            # pays for, and an exhausted balance stops the next read, not this one.
            # Recorded only when billed: the last message that the meter refuses still
            # goes to the caller, but it is not in the chain.
            billable = meter.add(len(data))
            if billable and receipts is not None:
                receipts.record(execution_receipt.DIRECTION_OUT, data)
            yield data
            if not billable:
                break  # Out of funds.
            if receipts is not None and receipts.due():
                checkpoint = receipts.checkpoint(mu_charged=meter.billed_mu)
                if checkpoint is not None:
                    yield checkpoint

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
        # The writer can still be accounting a message (a slow charge). Wait for it, so
        # the close below shows its charge and its message. Only with receipts: nothing
        # else reads the total.
        if receipts is not None and not meter.wait_idle(CLOSE_CHARGE_WAIT_S):
            logger(f"{LOG_PREFIX} {target}: a message is still being accounted after "
                   f"{CLOSE_CHARGE_WAIT_S:g}s; the close may differ from the ledger.")
        meter.settle()
        inbound.closed(stream_id)

    # Reached only when the relay ended on its own (the service closed, the caller
    # stopped, the balance ran out), not when it was closed from outside.
    if receipts is not None:
        close = receipts.checkpoint(mu_charged=meter.billed_mu, final=True)
        if close is not None:
            yield close


def service_tunnel(
    iterator: Iterator,
    is_active: Callable[[], bool] = lambda: True,
    caller: str = "",
) -> Tuple[object, Generator[bytes, None, None]]:
    """Establish a tunnel and return ``(conn, relay)``.

    Deliberately *not* a generator itself: the handshake, slot validation and
    connect all run eagerly on call, so a ``TunnelError`` surfaces before the
    gateway has serialized a single buffer and can still become a gRPC status.

    The connected socket is returned alongside the relay generator because it is
    opened eagerly, before the generator is iterated. ``_relay``'s own ``finally``
    closes it, but that only runs once the generator has been entered; if the
    consumer (``serialize_to_buffer``) fails before its first pull, the socket
    would leak. Handing ``conn`` back lets the caller close it unconditionally.
    For a relay to a peer, ``conn`` is the channel to the peer: it has ``close`` too.

    ``is_active`` is polled while relaying (the gateway passes
    ``context.is_active``) so a cancelled RPC tears the tunnel down instead of
    leaving it parked on an idle service.

    ``caller`` (the gateway passes ``context.peer()``) is who the stream is relayed
    for, as ``nodo tunnels --inbound`` lists it.

    A ``DescendantTunnel`` handshake goes to ``_descendant_tunnel`` (#631).
    """
    handshake = _handshake(iterator)
    if handshake.instance_ref:
        logger(f"{LOG_PREFIX} Handshake for descendant {handshake.instance_ref[:12]} "
               f"slot={handshake.slot}")
        return _descendant_tunnel(iterator, handshake, is_active, caller)
    logger(f"{LOG_PREFIX} Handshake for {_ref_label(handshake.token)} slot={handshake.slot}")
    return _open_local(iterator, handshake.token, handshake.slot, handshake.receipts,
                       is_active, caller)


def _open_local(
    iterator: Iterator,
    token: str,
    slot: str,
    want_receipts: bool,
    is_active: Callable[[], bool],
    caller: str,
    instance_ref: str = "",
) -> Tuple[socket.socket, Generator[bytes, None, None]]:
    """Open the tunnel to a local instance ``token``, which pays for it.

    ``instance_ref`` is set when an ancestor opens it (#631): the receipts and the
    ledger rows name the execution by it, and the operator list shows it in place of
    the token.
    """
    ip, port, transport = _resolve_target(token, slot)
    label = instance_ref[:12] if instance_ref else _ref_label(token)
    target = f"{label}@{ip}:{port}/{transport.value}"

    # A node with nothing left of its daily allowance (`host_limits`) refuses here
    # rather than handing out a socket it would close on the first message. Ahead of
    # `charge_open`, so nobody is billed for opening a tunnel this node was never going
    # to relay.
    if host_limits.daily_allowance_spent():
        raise TunnelExhausted(host_limits.daily_allowance_reason())

    receipts, cause = _receipts_and_cause(token, slot, want_receipts, instance_ref, target)

    # Charged before connecting: an instance that cannot pay to open the tunnel
    # gets a clean refusal instead of a socket it will lose mid-transfer.
    meter = TrafficMeter(token=token, target=target, cause=cause)
    if not meter.charge_open():
        raise TunnelExhausted(_NO_BALANCE)

    conn = _connect(ip, port, transport)
    logger(f"{LOG_PREFIX} {target}: tunnel established.")

    relay = _relay(
        iterator=iterator,
        conn=conn,
        is_active=is_active,
        target=target,
        is_udp=transport is TransportProtocol.UDP,
        meter=meter,
        stream={
            "caller": caller,
            "token": f"ref:{instance_ref}" if instance_ref else token,
            "slot": slot,
            "transport": transport.value,
            "target": f"{ip}:{port}",
        },
        receipts=receipts,
    )
    return conn, relay


def _receipts_and_cause(token: Optional[str], slot: str, want_receipts: bool,
                        instance_ref: str, target: str):
    """The receipt chain of a stream (None if not asked, or no key) and the cause of its
    ``tunnel`` rows.

    The cause names the execution, as the receipts do, and the stream of the receipts:
    a reader ties the charge to the receipt (#419). A stream of a ``DescendantTunnel``
    names the execution by ``instance_ref`` (#631).
    """
    receipts = None
    if want_receipts:
        receipts = execution_receipt.ReceiptChain.for_node(
            token or "", execution_receipt.PLANE_TUNNEL,
            execution_receipt.COVERAGE_CLIENT_TUNNEL, slot=slot,
            instance_ref=instance_ref or None,
        )
        if receipts is None:
            logger(f"{LOG_PREFIX} {target}: receipts asked, but this node has no identity key.")

    cause = {}
    if receipts is not None:
        cause = {"execution_name": receipts.name, "session": receipts.session}
    else:
        peer_id = execution_receipt.node_peer_id()
        if peer_id and instance_ref:
            cause = {"execution_name": execution_receipt.execution_name_by_ref(
                instance_ref, peer_id)}
        elif peer_id and token:
            cause = {"execution_name": execution_receipt.execution_name(token, peer_id)}
    return receipts, cause


def _tunnel_hops(remaining_hops: Optional[int]) -> int:
    """The hop budget of a tunnel: the one received, never above the node maximum."""
    from src.utils.tools.recursion_guard import max_hops
    limit = max_hops()
    return limit if remaining_hops is None else min(int(remaining_hops), limit)


def _descendant_tunnel(
    iterator: Iterator,
    handshake: Handshake,
    is_active: Callable[[], bool],
    caller: str,
):
    """Open a tunnel from an ancestor to a descendant (#631).

    The token of the handshake is the credential: an instance of this node, local or
    delegated, live or stopped (its row stays for a time after it stops). The node
    looks for the descendant below it (``balance_statement.locate``). It opens a local
    descendant itself. It relays to the peer of a delegated one.
    """
    from src.manager import balance_statement

    hops = _tunnel_hops(handshake.hops)
    if hops <= 0:
        raise TunnelExhausted(_NO_BUDGET)
    if sc.get_instance_row(handshake.token) is None:
        raise TunnelRefused(_NOT_LINKED)
    where = balance_statement.locate(handshake.token, handshake.instance_ref)
    found = where["found"]
    if found is not None and found["kind"] == "local":
        if not found["alive"]:
            raise TunnelUnavailable(_NOT_ALIVE)
        return _open_local(iterator, found["id"], handshake.slot, handshake.receipts,
                           is_active, caller, instance_ref=handshake.instance_ref)
    candidates = where["hops"]
    if not candidates:
        raise TunnelRefused(_NOT_LINKED)
    if found is not None and not found["alive"]:
        raise TunnelUnavailable(_NOT_ALIVE)
    budgets = balance_statement.split_hops(hops, len(candidates))
    if not budgets:
        raise TunnelExhausted(_NO_BUDGET)
    if len(candidates) == 1:
        (row, ask_ref), budget = candidates[0], budgets[0]
    else:
        row, ask_ref, budget = _choose_peer(list(zip(candidates, budgets)))
    # The descendant pays on this node when this node holds its account: the alias of a
    # delegated descendant. A descendant further down has no account here.
    payer = found["id"] if found is not None else None
    return _open_peer(iterator, handshake, row, ask_ref, budget, payer, is_active, caller)


def _choose_peer(candidates: List) -> Tuple[dict, str, int]:
    """Find the one peer of ``candidates`` that holds the descendant.

    A tunnel cannot try one peer after the other, as the read does: the refusal of a
    peer comes only when the stream runs. The node does not ask the peers here: a call
    to each peer costs this node MU on that peer, and the caller of a tunnel pays
    nothing before the tunnel opens. The node uses what a read of the statement of the
    descendant found (``balance_statement._answer_descendant``): it keeps the head of the
    statement for the one peer that gave it. A read is charged to its caller.

    Raises ``TunnelRefused`` when no peer gave a statement of the descendant: the
    caller reads the statement first. Raises ``TunnelUnavailable`` when more than one
    peer gave one: a peer can sign a statement for a descendant that it does not hold,
    and the node does not send the bytes of the caller to a peer that can be the wrong
    one.
    """
    known = [((row, ask_ref), budget) for (row, ask_ref), budget in candidates
             if sc.descendant_head(row["peer_id"], ask_ref) is not None]
    if not known:
        raise TunnelRefused(_READ_FIRST)
    if len(known) > 1:
        logger(f"{LOG_PREFIX} {len(known)} peers gave a statement of descendant "
               f"{known[0][0][1][:12]}; no tunnel.")
        raise TunnelUnavailable(_MANY_BELOW)
    (row, ask_ref), budget = known[0]
    return row, ask_ref, budget


def _peer_channel(peer_id: str):
    """A verified channel to ``peer_id``. A function of its own, so a test can replace it."""
    from src.identity.grpc_transport import peer_channel
    return peer_channel(peer_id)


def _open_peer(
    iterator: Iterator,
    handshake: Handshake,
    row: dict,
    ask_ref: str,
    budget: int,
    payer: Optional[str],
    is_active: Callable[[], bool],
    caller: str,
):
    """Relay a ``DescendantTunnel`` to the peer of the delegated instance ``row``.

    This node uses the token that the peer gave it for the instance
    (``token_delegation``) and sends ``ask_ref`` with the hop budget ``budget``. It
    meters its own leg: against ``payer`` (an alias) when it has one, else only against
    the operator's ceilings. The open charge is given back when the next node refuses
    the tunnel before it relays a byte (``TrafficMeter.refund_open``).

    This node signs its own leg for its caller. It asks the next node for receipts
    too, and keeps the last valid one for the peer and ``ask_ref``: it is the proof
    that the descendant answered at that time (``_relay_peer``). It does not give them
    to its caller: they name the next node.
    """
    peer_id = row["peer_id"]
    label = handshake.instance_ref[:12]
    target = f"{label}>peer/{handshake.slot}"

    if host_limits.daily_allowance_spent():
        raise TunnelExhausted(host_limits.daily_allowance_reason())

    try:
        channel = _peer_channel(peer_id)
    except Exception as e:
        # The text of the error names the peer: it stays in the log of the node.
        logger(f"{LOG_PREFIX} {target}: no channel to {peer_id}: {e}")
        _record_hop(peer_id, ask_ref, ok=False)
        raise TunnelUnavailable(_BELOW_FAILED)

    receipts, cause = _receipts_and_cause(None, handshake.slot, handshake.receipts,
                                          handshake.instance_ref, target)
    meter = TrafficMeter(token=payer, target=target, cause=cause, delegated=True)
    if not meter.charge_open():
        channel.close()
        raise TunnelExhausted(_NO_BALANCE)
    logger(f"{LOG_PREFIX} {target}: relaying to {peer_id}.")

    first = celaut_pb2.DescendantTunnel(
        token=row["token_delegation"], instance_ref=ask_ref, slot=handshake.slot,
        receipts=True, remaining_hops=int(budget),
    )
    relay = _relay_peer(
        iterator=iterator, channel=channel, first=first, is_active=is_active,
        target=target, meter=meter, peer_id=peer_id, ask_ref=ask_ref,
        head=lambda: _refresh_head(row, ask_ref, budget, payer),
        stream={
            "caller": caller,
            "token": f"ref:{handshake.instance_ref}",
            "slot": handshake.slot,
            "transport": "relay",
            "target": "peer",
        },
        receipts=receipts,
    )
    return channel, relay


def _record_hop(peer_id: str, ask_ref: str, ok: bool) -> None:
    """Write the result of a hop for the judgement of the peer. A failure to write is
    logged and does not stop the tunnel."""
    try:
        sc.record_descendant_tunnel(peer_id, ask_ref, ok=ok)
    except Exception as e:
        logger(f"{LOG_PREFIX} Cannot write the hop to {peer_id}: {e}")


def _valid_receipt(message, peer_id: str, ask_ref: str) -> Optional[bytes]:
    """``message`` serialized, when it is a receipt that ``peer_id`` signed for the
    execution that ``ask_ref`` names. None for any other message."""
    if not isinstance(message, celaut_pb2.ExecutionReceipt):
        return None
    try:
        if execution_receipt.problems(message, peer_id=peer_id, instance_ref=ask_ref):
            return None
        return message.SerializeToString()
    except Exception as e:
        logger(f"{LOG_PREFIX} A receipt of {peer_id} does not verify: {e}")
        return None


def _keep_receipt(peer_id: str, ask_ref: str, receipt: bytes) -> None:
    """Keep the last receipt of the peer for the hop. A failure to write is logged."""
    try:
        sc.record_descendant_receipt(peer_id, ask_ref, receipt)
    except Exception as e:
        logger(f"{LOG_PREFIX} Cannot keep the receipt of {peer_id}: {e}")


# A hop that works reads the head of the statement of the descendant again only when
# the head that this node keeps is older than this (seconds). So the reads that this
# node pays on the peer have a limit for each descendant.
HEAD_REFRESH_S = 3600


def _refresh_head(row: dict, ask_ref: str, budget: int, payer: Optional[str]) -> None:
    """Read the head of the statement of the descendant from the peer of ``row`` after
    a hop that worked, and keep it (#631): with the receipt, it shows the state of the
    descendant when it answered.

    The read has no rows (``from_seq`` 0). It is charged to ``payer`` (the alias of the
    descendant) when this node holds one, else this node pays it. It runs at most once
    in ``HEAD_REFRESH_S`` for each peer and descendant. Each error is logged only.
    """
    from src.database import ledger
    from src.manager import balance_statement, statement_check

    peer_id = row["peer_id"]
    try:
        kept = sc.descendant_head(peer_id, ask_ref)
        if kept is not None and time.time() - kept["head_ts"] < HEAD_REFRESH_S:
            return
        client_id = balance_statement._client_on(peer_id)
        if payer:
            sc.move_balance(payer, -balance_statement.statement_price_mu(0),
                            ledger.STATEMENT_QUERY, allow_debt=False,
                            detail={"descendant": ask_ref, "from_seq": "0", "to_seq": "0",
                                    "rows": "0"})
        statement = statement_check.read(peer_id, client_id, token=row["token_delegation"],
                                         from_seq=0, instance_ref=ask_ref,
                                         remaining_hops=budget)
        sc.record_descendant_head(peer_id, ask_ref, statement.head_seq, statement.head_hash,
                                  ts=statement.ts or None)
    except Exception as e:
        logger(f"{LOG_PREFIX} No head of descendant {ask_ref[:12]} from {peer_id}: {e}")


def _status_of(error: Exception):
    code = getattr(error, "code", None)
    try:
        return code() if callable(code) else None
    except Exception:
        return None


def _below_error(error: Exception) -> TunnelError:
    """The error of the next node, as this node gives it to its caller.

    The text of the next node is not passed on: it can be about a node that the
    caller must not know. Only the class goes up.
    """
    import grpc
    code = _status_of(error)
    if code == grpc.StatusCode.PERMISSION_DENIED:
        return TunnelRefused(_NOT_LINKED)
    if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return TunnelExhausted("A node below has no traffic allowance, balance or hop budget left.")
    if code == grpc.StatusCode.INVALID_ARGUMENT:
        return TunnelError(_BELOW_REFUSED)
    return TunnelUnavailable(_BELOW_FAILED)


def _is_unavailable(error: Exception) -> bool:
    """Whether ``error`` of the next node says that the descendant does not answer."""
    import grpc
    if isinstance(error, (ConnectionError, OSError)):
        return True
    return _status_of(error) in (grpc.StatusCode.UNAVAILABLE,
                                 grpc.StatusCode.DEADLINE_EXCEEDED)


def _relay_peer(
    iterator: Iterator,
    channel,
    first,
    is_active: Callable[[], bool],
    target: str,
    meter: TrafficMeter,
    peer_id: str,
    ask_ref: str,
    stream: Optional[dict] = None,
    receipts: Optional[execution_receipt.ReceiptChain] = None,
    head: Optional[Callable[[], None]] = None,
) -> Generator[bytes, None, None]:
    """Relay the bytes of the caller to the next node, and its bytes back (#631).

    The gRPC client consumes the outbound generator on its own thread, so there is no
    writer thread here. Each message is metered before it goes on, in both directions,
    as ``_relay`` does, and only a message that the meter accepted is in the chains of
    the receipts. A receipt of the next node does not go to the caller. The last one
    that is valid for the peer and ``ask_ref`` is kept at the close
    (``SQLConnection.record_descendant_receipt``).

    The first byte from the next node writes the hop as one that worked. After the
    close of a hop that worked, ``head`` reads the head of the statement of the
    descendant, on a thread of its own. An error of the next node before a byte
    writes the hop as failed when it says that the descendant does not answer, and
    gives back the open charge. The error then goes up as a ``TunnelError`` with a
    text of this node (``_below_error``), after the close of the leg.

    A watcher thread checks ``is_active`` each ``POLL_INTERVAL_S``. When the caller
    goes, it closes the channel: that cancels the leg to the next node, also while no
    message comes from it.
    """
    from src.utils.bee_client import BeeClient

    stream_id = inbound.opened(**stream) if stream else None
    stop = threading.Event()
    cancelled = threading.Event()

    def watch():
        while not stop.wait(POLL_INTERVAL_S):
            if not is_active():
                cancelled.set()
                logger(f"{LOG_PREFIX} {target}: the caller went; cancelling the leg.")
                try:
                    channel.close()
                except Exception:
                    pass
                return

    threading.Thread(target=watch, name="descendant-relay-watch", daemon=True).start()

    def outbound():
        yield first
        sent = 0
        try:
            for message in iterator:
                if stop.is_set():
                    return
                if not isinstance(message, bytes):
                    logger(f"{LOG_PREFIX} {target}: ignoring unexpected "
                           f"{type(message).__name__} in the payload stream.")
                    continue
                with meter.accounting():
                    if not meter.add(len(message)):
                        return  # Out of funds. Not billed, so not in the receipt chain.
                    if receipts is not None:
                        receipts.record(execution_receipt.DIRECTION_IN, message)
                sent += len(message)
                inbound.count(stream_id, bytes_in=len(message))
                yield message
        except Exception as e:
            if not stop.is_set():
                logger(f"{LOG_PREFIX} {target}: caller -> peer stopped: {e}")
        finally:
            logger(f"{LOG_PREFIX} {target}: caller -> peer closed after {sent} bytes.")

    received = 0
    worked = False
    failure: Optional[TunnelError] = None
    proof: Optional[bytes] = None
    try:
        responses = BeeClient.service_tunnel(channel, outbound())
        for message in responses:
            if not is_active() or meter.exhausted.is_set():
                break
            if not isinstance(message, bytes):
                # The receipts of the next node name it: they do not go to the caller.
                # This node signs its own leg.
                proof = _valid_receipt(message, peer_id, ask_ref) or proof
                continue
            if not worked:
                worked = True
                _record_hop(peer_id, ask_ref, ok=True)
            received += len(message)
            inbound.count(stream_id, bytes_out=len(message))
            billable = meter.add(len(message))
            if billable and receipts is not None:
                receipts.record(execution_receipt.DIRECTION_OUT, message)
            yield message
            if not billable:
                break  # Out of funds.
            if receipts is not None and receipts.due():
                checkpoint = receipts.checkpoint(mu_charged=meter.billed_mu)
                if checkpoint is not None:
                    yield checkpoint
    except Exception as e:
        # The text can name the peer or its address: it stays in the log of this node.
        logger(f"{LOG_PREFIX} {target}: {peer_id} stopped the stream: {e}")
        # A leg that the watcher cancelled is no failure of the peer: the caller went.
        if not worked and not cancelled.is_set():
            if _is_unavailable(e):
                _record_hop(peer_id, ask_ref, ok=False)
            failure = _below_error(e)
    finally:
        logger(f"{LOG_PREFIX} {target}: peer -> caller closed after {received} bytes.")
        stop.set()
        try:
            channel.close()
        except Exception:
            pass
        if receipts is not None and not meter.wait_idle(CLOSE_CHARGE_WAIT_S):
            logger(f"{LOG_PREFIX} {target}: a message is still being accounted after "
                   f"{CLOSE_CHARGE_WAIT_S:g}s; the close may differ from the ledger.")
        meter.settle()
        if failure is not None:
            # The next node refused the tunnel before a byte: nobody pays to open it.
            meter.refund_open()
        if proof is not None:
            _keep_receipt(peer_id, ask_ref, proof)
        if worked and head is not None:
            threading.Thread(target=head, name="descendant-head", daemon=True).start()
        inbound.closed(stream_id)

    if failure is not None:
        raise failure
    if receipts is not None:
        close = receipts.checkpoint(mu_charged=meter.billed_mu, final=True)
        if close is not None:
            yield close
