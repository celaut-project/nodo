"""Local endpoints for services running on a peer we cannot address directly.

When execution is delegated, the peer answers with an ``Instance`` whose
``uri_slot`` holds *its* addresses. Our client — usually a microVM on our bridge —
then connects straight to them. That works only while those addresses are
reachable from here, and there are ordinary configurations where they are not:

* the peer runs with ``network.DISABLE_EXPOSE_OUTSIDE``, so it advertises the
  internal IP of its own bridge, which is meaningless outside its host;
* the peer is behind NAT and advertises a LAN address we do not share.

In those cases we terminate the connection ourselves: one local listener per
declared slot, each tunnelling to the peer over ``Gateway.ServiceTunnel``, and the
``uri_slot`` we hand to our client is rewritten to point at those listeners. The
client keeps speaking its own protocol to what looks like a local service.

Why the proxy has to be on *our* side: the client speaks its service's protocol,
not beeRPC. The peer cannot hand it anything tunnelled, so only the caller's node
can offer a plain socket and do the encapsulating.

Policy (``network.DELEGATION_TUNNEL_POLICY``)
--------------------------------------------
* ``auto`` (default) — tunnel only when the peer's advertised addresses do not
  answer from here. A client on the same network as the peer keeps talking to it
  directly, with no extra hop. Reachability is probed from *this node*, which is
  an approximation of what the client can reach, and UDP slots cannot be probed
  at all (no handshake), so they are tunnelled.
* ``always`` — tunnel every delegated instance. No guessing, one extra hop.
* ``never`` — always hand over the peer's own addresses. A client on another
  network will fail to connect.

State
-----
Listeners live as long as the delegated instance. The rewritten instance is what
gets stored in ``delegated_instances.serialized_instance`` — what the client was
told is what we persist — so ``restore()`` can rebuild the same listeners on the
same ports after a node restart, and firewall cleanup on stop targets the address
the client was actually given.
"""

import random
import socket
import threading
from typing import Dict, List, Optional, Tuple

import netifaces as ni

from protos import celaut_pb2
from src.database.sql_connection import SQLConnection
from src.tunneling import logger
from src.tunneling.tunnel_client import serve_tcp, serve_udp
from src.utils import utils
from src.utils.config import ConfigManager
from src.virtualizers.firewall import TransportProtocol, resolve_slot_transport_protocols

sc = SQLConnection()
env_manager = ConfigManager()

LOG_PREFIX = "[TUNNEL][DELEGATED]"

# How long close() waits for a serving thread to notice it should stop.
ENDPOINT_JOIN_TIMEOUT_S = 2.0

POLICY_AUTO = "auto"
POLICY_ALWAYS = "always"
POLICY_NEVER = "never"
VALID_POLICIES = (POLICY_AUTO, POLICY_ALWAYS, POLICY_NEVER)


class _Endpoint:
    """One local listener standing in for one remote slot."""

    def __init__(
        self,
        listener: socket.socket,
        stop: threading.Event,
        thread: threading.Thread,
        internal_port: int,
        local_port: int,
    ) -> None:
        self.listener = listener
        self.stop = stop
        self.thread = thread
        self.internal_port = internal_port
        self.local_port = local_port

    def close(self) -> None:
        """Release the port, and do not return until it really is released.

        Closing the socket is not enough on its own: the serving thread is parked
        inside accept()/recvfrom(), and the kernel keeps the socket listening until
        that call returns — up to a poll interval during which it would still
        accept clients for an instance that is already gone. shutdown() makes the
        blocked call return at once (on Linux it fails with EINVAL, which the
        serve loop treats as "listener closed").
        """
        self.stop.set()
        for teardown in (
            lambda: self.listener.shutdown(socket.SHUT_RDWR),
            self.listener.close,
        ):
            try:
                teardown()
            except OSError:
                pass

        self.thread.join(timeout=ENDPOINT_JOIN_TIMEOUT_S)
        if self.thread.is_alive():
            logger(
                f"{LOG_PREFIX} Endpoint thread for slot {self.internal_port} did not "
                f"stop within {ENDPOINT_JOIN_TIMEOUT_S}s."
            )


# token as the peer knows it -> its local endpoints.
_endpoints: Dict[str, List[_Endpoint]] = {}
_endpoints_lock = threading.Lock()

# Per-token publish locks. publish() opens listeners *outside* _endpoints_lock,
# so two concurrent publishes for the same token would both bind and both
# register, orphaning a generation of listeners (and colliding on pinned ports).
# Serialise the whole check->close->open->register per token, while still letting
# different tokens publish in parallel.
_publish_locks: Dict[str, threading.Lock] = {}
_publish_locks_guard = threading.Lock()


def _publish_lock(token: str) -> threading.Lock:
    with _publish_locks_guard:
        lock = _publish_locks.get(token)
        if lock is None:
            lock = threading.Lock()
            _publish_locks[token] = lock
        return lock


def _policy() -> str:
    configured = str(env_manager.get("network.DELEGATION_TUNNEL_POLICY", POLICY_AUTO) or "").strip().lower()
    if configured in VALID_POLICIES:
        return configured
    logger(
        f"{LOG_PREFIX} network.DELEGATION_TUNNEL_POLICY='{configured}' is not one of "
        f"{VALID_POLICIES}; falling back to '{POLICY_AUTO}'."
    )
    return POLICY_AUTO


def _slot_transports(instance: celaut_pb2.Instance) -> Dict[int, Optional[TransportProtocol]]:
    """Transport declared for each API slot port."""
    transports: Dict[int, Optional[TransportProtocol]] = {}
    for api_slot in instance.api.slot:
        try:
            transports[api_slot.port] = resolve_slot_transport_protocols(
                api_slot, logger_fn=logger, context=LOG_PREFIX
            )
        except ValueError as e:
            logger(f"{LOG_PREFIX} Slot {api_slot.port} has an unusable transport: {e}")
            transports[api_slot.port] = None
    return transports


def _is_reachable(instance: celaut_pb2.Instance) -> bool:
    """Does every slot the service declares answer at one of its addresses?

    Which slots exist comes from ``instance.api.slot`` -- the service's own
    specification, echoed back unconditionally by whoever ran it -- not from
    ``instance.uri_slot``, which only lists what the peer *chose* to expose. A
    peer that could not give an address for a declared slot (no ``Uri_Slot`` for
    it at all, e.g. it has no local network in common with us and no public IP
    configured) is exactly the case that must count as unreachable.
    """
    transports = _slot_transports(instance)
    given = {uri_slot.internal_port: uri_slot for uri_slot in instance.uri_slot}

    for internal_port, transport in transports.items():
        if transport is not TransportProtocol.TCP:
            logger(
                f"{LOG_PREFIX} Slot {internal_port} is "
                f"{transport.value if transport else 'untyped'}; cannot probe it, "
                "treating as unreachable."
            )
            return False

        uri_slot = given.get(internal_port)
        if uri_slot is None or not uri_slot.uri:
            logger(f"{LOG_PREFIX} Slot {internal_port} was given no address at all.")
            return False

        if not any(utils.is_open(ip=uri.ip, port=uri.port) for uri in uri_slot.uri):
            logger(
                f"{LOG_PREFIX} Slot {internal_port} does not answer at "
                f"{[f'{uri.ip}:{uri.port}' for uri in uri_slot.uri]}."
            )
            return False

    return True


def should_tunnel(instance: celaut_pb2.Instance) -> bool:
    """Apply ``network.DELEGATION_TUNNEL_POLICY`` to a peer's instance."""
    policy = _policy()

    if policy == POLICY_NEVER:
        return False
    if policy == POLICY_ALWAYS:
        return True

    if not instance.api.slot:
        return False  # The service itself declares no API slots; nothing to stand in for.

    return not _is_reachable(instance)


def advertise_ip_for(father_id: str, father_ip: str) -> Optional[str]:
    """Which of our addresses the client should be told to connect to.

    Mirrors how ``local_execution`` picks the IP it publishes: a microVM client
    reaches us across the virtualizer bridge, anything else reaches us on the
    network it came from.
    """
    if father_id and sc.internal_instance_exists(id=father_id):
        # Imported lazily: the network module reaches the database and the firewall.
        from src.virtualizers.microvm.network import NETWORK_BRIDGE_NAME

        try:
            return utils.get_local_ip_from_network(
                network=NETWORK_BRIDGE_NAME, allow_link_local=False
            )
        except Exception as e:
            logger(f"{LOG_PREFIX} Cannot resolve our IP on {NETWORK_BRIDGE_NAME}: {e}")
            return None

    try:
        network = utils.get_network_name(direction=father_ip)
        if network == "localhost":
            return "127.0.0.1"
        if network is None:
            logger(f"{LOG_PREFIX} {father_ip} is not on any of our own networks.")
            return None
        return utils.get_local_ip_from_network(network=network, allow_link_local=False)
    except Exception as e:
        logger(f"{LOG_PREFIX} Cannot resolve our IP towards {father_ip}: {e}")
        return None


def _bind_on_interface(
    listener: socket.socket,
    bind_ip: str,
    local_port: Optional[int],
    is_udp: bool,
) -> Optional[int]:
    """Bind ``listener`` on ``bind_ip`` and return the port it got, or None.

    Binding is the allocation: the kernel hands out a free port on the exact
    interface in a single syscall, so nothing can steal it between pick and use.
    A pinned ``local_port`` must rebind that exact port (restoring an endpoint a
    client already knows); otherwise, if ``network.FREE_PORTS_RANGE`` restricts
    which ports the firewall forwards, try those on the interface directly (the
    bind is the check); with no range, let the kernel pick an ephemeral port.
    """
    def _finish() -> int:
        if not is_udp:
            listener.listen(16)
        return int(listener.getsockname()[1])

    if local_port:
        try:
            listener.bind((bind_ip, local_port))
            return _finish()
        except OSError as e:
            logger(f"{LOG_PREFIX} Cannot rebind {bind_ip}:{local_port}: {e}")
            return None

    candidates: List[int] = []
    for r in env_manager.get("network.FREE_PORTS_RANGE", []) or []:
        try:
            start, end = int(r["START"]), int(r["END"])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= end:
            candidates.extend(range(start, end + 1))

    if candidates:
        random.shuffle(candidates)
        for candidate in candidates:
            try:
                listener.bind((bind_ip, candidate))
                return _finish()
            except OSError:
                continue
        logger(f"{LOG_PREFIX} No free port on {bind_ip} within FREE_PORTS_RANGE.")
        return None

    try:
        listener.bind((bind_ip, 0))
        return _finish()
    except OSError as e:
        logger(f"{LOG_PREFIX} Cannot bind an ephemeral port on {bind_ip}: {e}")
        return None


def _open_endpoint(
    token: str,
    internal_port: int,
    transport: TransportProtocol,
    peer_gateway: str,
    bind_ip: str,
    local_port: Optional[int] = None,
    peer_id: Optional[str] = None,
) -> Optional[_Endpoint]:
    """Bind one local listener that tunnels to ``internal_port`` on the peer."""
    if ":" in bind_ip:
        # IPv6 is not implemented on this path yet. Skip explicitly instead of
        # creating an AF_INET socket, letting bind() raise, and silently handing
        # the client back the peer's unreachable v6 address.
        logger(
            f"{LOG_PREFIX} IPv6 bind_ip {bind_ip} is unsupported for delegated "
            f"endpoints; skipping slot {internal_port}."
        )
        return None

    is_udp = transport is TransportProtocol.UDP
    listener = socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM if is_udp else socket.SOCK_STREAM
    )
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Bind on bind_ip itself — never the wildcard address — so the port is
    # acquired atomically on the exact interface the client was told to use.
    # The bind IS the allocation, which removes the check-then-bind race and the
    # wildcard-vs-interface mismatch of picking a "free" port and binding it in
    # two steps. (A stand-in for a remote service must also not widen what this
    # host exposes.)
    port = _bind_on_interface(listener, bind_ip, local_port, is_udp)
    if port is None:
        logger(f"{LOG_PREFIX} Could not bind a listener on {bind_ip} for slot {internal_port}.")
        listener.close()
        return None

    stop = threading.Event()
    serve = serve_udp if is_udp else serve_tcp
    kwargs = {
        "listener": listener,
        "token": token,
        "slot": internal_port,
        "gateway": peer_gateway,
        "log": logger,
        "should_stop": stop,
        # Whose gateway peer_gateway is supposed to be, so the relay's TLS channel is
        # pinned to that node (issue #257) rather than to whoever holds the address:
        # this stream carries the delegated token, which is a capability.
        "expected_peer_id": peer_id,
    }
    thread = threading.Thread(
        target=serve,
        kwargs=kwargs,
        name=f"delegated-endpoint-{token[:8]}-{internal_port}",
        daemon=True,
    )
    thread.start()

    logger(
        f"{LOG_PREFIX} {bind_ip}:{port}/{transport.value} -> slot {internal_port} "
        f"of {token} via {peer_gateway}"
    )
    return _Endpoint(
        listener=listener,
        stop=stop,
        thread=thread,
        internal_port=internal_port,
        local_port=port,
    )


def publish(
    token: str,
    peer_gateway: str,
    instance: celaut_pb2.Instance,
    bind_ip: str,
    port_by_slot: Optional[Dict[int, int]] = None,
    peer_id: Optional[str] = None,
) -> celaut_pb2.Instance:
    """Stand in for ``instance``'s slots locally and return the rewritten instance.

    ``token`` must be the token as the *peer* knows it, since that is what its
    ServiceTunnel validates. ``port_by_slot`` pins local ports when restoring
    endpoints that a client already knows about.
    """
    with _publish_lock(token):
        return _publish_locked(
            token, peer_gateway, instance, bind_ip, port_by_slot, peer_id
        )


def _publish_locked(
    token: str,
    peer_gateway: str,
    instance: celaut_pb2.Instance,
    bind_ip: str,
    port_by_slot: Optional[Dict[int, int]] = None,
    peer_id: Optional[str] = None,
) -> celaut_pb2.Instance:
    # Replace any previous generation of endpoints for this token so a retried
    # restore() or a re-delegation cannot orphan the earlier listeners.
    if endpoint_count(token):
        close(token)

    transports = _slot_transports(instance)
    given = {uri_slot.internal_port: uri_slot for uri_slot in instance.uri_slot}
    rewritten = celaut_pb2.Instance()
    rewritten.api.CopyFrom(instance.api)

    opened: List[_Endpoint] = []
    # Iterate the service's own declared slots, not instance.uri_slot: a slot the
    # peer could not give any address for (no local network in common, no public
    # IP configured) is precisely the one that most needs a tunnel, and it may
    # not have an entry in instance.uri_slot at all.
    for internal_port, transport in transports.items():
        peer_uri_slot = given.get(internal_port)
        if transport is None:
            if peer_uri_slot is not None:
                logger(
                    f"{LOG_PREFIX} Slot {internal_port} of {token} has no usable "
                    "transport; leaving its addresses untouched."
                )
                rewritten.uri_slot.append(peer_uri_slot)
            continue

        endpoint = _open_endpoint(
            token=token,
            internal_port=internal_port,
            transport=transport,
            peer_gateway=peer_gateway,
            bind_ip=bind_ip,
            local_port=(port_by_slot or {}).get(internal_port),
            peer_id=peer_id,
        )
        if endpoint is None:
            # Better to advertise the peer's own address, if it gave one, than nothing at all.
            if peer_uri_slot is not None:
                rewritten.uri_slot.append(peer_uri_slot)
            continue

        opened.append(endpoint)
        rewritten.uri_slot.append(
            celaut_pb2.Instance.Uri_Slot(
                internal_port=internal_port,
                uri=[celaut_pb2.Instance.Uri(ip=bind_ip, port=endpoint.local_port)],
            )
        )

    if opened:
        with _endpoints_lock:
            # close() above already cleared any prior generation, so replace.
            _endpoints[token] = opened

    return rewritten


def endpoint_count(token: str) -> int:
    """How many local endpoints are currently standing in for ``token``."""
    with _endpoints_lock:
        return len(_endpoints.get(token, []))


def close(token: str) -> None:
    """Close every local endpoint standing in for ``token``."""
    with _endpoints_lock:
        endpoints = _endpoints.pop(token, [])

    for endpoint in endpoints:
        endpoint.close()

    if endpoints:
        logger(f"{LOG_PREFIX} Closed {len(endpoints)} endpoint(s) for {token}.")


def restore() -> int:
    """Re-open endpoints for delegated instances recorded before a restart.

    The stored instance is the rewritten one, so its ``uri_slot`` already says
    which local ports the client expects; they are re-bound as they were, because
    a client holding the old address cannot be told about a new port.

    Returns the number of endpoints actually reopened. Instances whose ports could
    not be rebound are logged individually — their clients are broken until the
    port frees up, and a silent count would hide that.
    """
    restored = 0
    unrecovered = 0
    for row in sc.get_delegated_instances():
        serialized = row.get('serialized_instance')
        token = row.get('token')
        peer_id = row.get('peer_id')
        if not serialized or not token or not peer_id:
            continue

        instance = celaut_pb2.Instance()
        try:
            instance.ParseFromString(serialized)
        except Exception as e:
            logger(f"{LOG_PREFIX} Stored instance for {token} is unreadable: {e}")
            continue

        local_addresses = _local_addresses(instance)
        if not local_addresses:
            continue  # Never tunnelled; the client talks to the peer directly.

        try:
            peer_gateway = next(utils.generate_uris_by_peer_id(peer_id))
        except StopIteration:
            logger(f"{LOG_PREFIX} No reachable address for peer {peer_id}; skipping {token}.")
            continue

        bind_ip, port_by_slot = local_addresses
        publish(
            token=token,
            peer_gateway=peer_gateway,
            instance=instance,
            bind_ip=bind_ip,
            port_by_slot=port_by_slot,
            peer_id=peer_id,
        )

        # Count what actually came up, not rows processed: a pinned port can fail
        # to rebind (something took it while the node was down) and then the
        # client's address leads nowhere. The stored record is deliberately left
        # alone — it holds the port the client was given, so a later restart can
        # still recover the tunnel once that port frees up. Overwriting it would
        # throw away the only thing that makes recovery possible.
        opened = endpoint_count(token)
        if opened:
            restored += opened
            continue

        logger(
            f"{LOG_PREFIX} Could not reopen any endpoint for delegated instance "
            f"{token}: its client still points at {bind_ip}:"
            f"{sorted(port_by_slot.values())} where nothing is listening now. "
            "Freeing that port and restarting the node recovers it."
        )
        unrecovered += 1

    if restored or unrecovered:
        logger(
            f"{LOG_PREFIX} Restored {restored} delegated endpoint(s)"
            + (f"; {unrecovered} instance(s) could not be recovered." if unrecovered else ".")
        )
    return restored


def _local_addresses(
    instance: celaut_pb2.Instance,
) -> Optional[Tuple[str, Dict[int, int]]]:
    """Extract ``(bind_ip, {internal_port: local_port})`` if this instance was tunnelled.

    A stored instance is recognised as tunnelled when its addresses are ours: the
    node only ever advertises one of its own IPs when it stood in for the peer.
    """
    ports: Dict[int, int] = {}
    bind_ip: Optional[str] = None

    for uri_slot in instance.uri_slot:
        for uri in uri_slot.uri:
            if not _is_own_address(uri.ip):
                continue
            bind_ip = uri.ip
            ports[uri_slot.internal_port] = uri.port
            break

    if not ports or bind_ip is None:
        return None
    return bind_ip, ports


def _is_own_address(ip: str) -> bool:
    """Is ``ip`` an address of this host?

    Compared against the interfaces' own addresses, not merely their networks: a
    peer sitting in our LAN shares our network but is not us.
    """
    if ip.startswith("127."):
        return True

    try:
        for interface in ni.interfaces():
            for address in ni.ifaddresses(interface).get(ni.AF_INET, []):
                if address.get("addr") == ip:
                    return True
    except Exception as e:
        logger(f"{LOG_PREFIX} Cannot enumerate local addresses: {e}")

    return False
