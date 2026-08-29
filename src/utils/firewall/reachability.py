"""Prove the guest paths work, instead of assuming they do.

Two questions, both answered with a real packet: can a guest reach the gateway on
this host (``probe_tcp_from_bridge``), and can a guest reach another guest
(``probe_tcp_between_guests``). Both cross a netfilter hook nodo shares with every
other firewall on the box, and on both an ``accept`` in nodo's own table settles
nothing -- see the module docstring of ``backends.py``.

The incident this exists for: a correct ``iptables ... -j ACCEPT`` rule for the
gateway port sat in the ruleset for two days while every guest's gRPC call was
rejected by a higher-priority foreign chain. The rule was present, the port was
closed, and nothing noticed -- because nothing ever tried.

Checking from the host itself proves nothing: a connect to a local address goes
out over ``lo``, which almost every firewall accepts unconditionally. The only
faithful test is to send the packet the way a guest does, in on the guest bridge.
So we build a throwaway network namespace, hand it a veth whose peer is enslaved
to that bridge, and connect from inside it. Same interface, same source subnet,
same input hook, same verdict.

Root-only, and deliberately best-effort: when the bridge does not exist yet (a
fresh node that has never run an instance) the answer is "unknown", not "closed".
The same goes for a port nothing is listening on -- see ``_listener_present``.

The namespace is entered with ``nsenter``, not with iproute2's own switch, for the
reason spelled out in ``NamespaceEntry``: ``ip -n`` remounts /sys, and a host that
forbids that mount could never verify its own gateway port.
"""

import contextlib
import ipaddress
import os
import random
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Set

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

PROBE_ATTEMPTS = 3
PROBE_CONNECT_TIMEOUT_S = 3.0
# A freshly enslaved bridge port needs a moment before it forwards.
PROBE_RETRY_DELAY_S = 0.5


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one reachability probe.

    ``reachable`` is tri-state on purpose: ``None`` means the probe could not be
    performed, which must never be reported as a closed port.
    """

    reachable: Optional[bool]
    detail: str
    source_ip: Optional[str] = None

    @property
    def conclusive(self) -> bool:
        return self.reachable is not None

    def __str__(self) -> str:
        if self.reachable is True:
            return f"reachable ({self.detail})"
        if self.reachable is False:
            return f"NOT reachable ({self.detail})"
        return f"unknown ({self.detail})"


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(command), capture_output=True, text=True, check=False)


_CONNECT_SNIPPET = (
    "import socket,sys\n"
    "s=socket.socket()\n"
    "s.settimeout(float(sys.argv[3]))\n"
    "try:\n"
    "    s.connect((sys.argv[1], int(sys.argv[2])))\n"
    "    print('connected')\n"
    "except Exception as e:\n"
    "    print(type(e).__name__ + ': ' + str(e))\n"
    "    sys.exit(1)\n"
    "finally:\n"
    "    s.close()\n"
)


def _listener_present(port: int) -> bool:
    """Is anything on this host bound to ``port``?

    The probe can only separate a firewall verdict from an empty port when there
    is something on the other end to answer. Without a listener the connect fails
    either way, and the failure looks the same: a port nobody is bound to answers
    with a RST, and so does a reject rule. So "no listener" is "unknown", never
    "closed" -- which is why the gateway is probed only after it starts listening.

    Bind rather than parse ``ss``: the question is precisely whether a bind here
    would collide. gRPC binds its listener with SO_REUSEPORT, but a bind that does
    not set it collides all the same.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("", port))
        except OSError:
            return True
    return False


@contextlib.contextmanager
def _temporary_listener(port: int):
    """Hold ``port`` open for the duration of a probe, or yield False if we cannot.

    This is what makes a port checkable *before* the gateway exists. Without a
    listener the probe has to answer "unknown" (see ``_listener_present``), which
    is why port assignment used to skip verification entirely and persist a port
    nothing had ever tried to reach. A throwaway socket is enough: the kernel
    completes the handshake from the accept queue, so the probe gets the same
    verdict a real listener would give it -- the firewall does not know the
    difference.

    Binds the wildcard address, because the probe connects to the bridge's gateway
    IP, not to loopback.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        sock.listen(8)
    except OSError:
        sock.close()
        yield False
        return
    try:
        yield True
    finally:
        sock.close()


# Where ``ip netns add`` publishes the namespace it just created. /var/run is a
# symlink to /run on any host this runs on, but both are looked at rather than
# assumed.
NETNS_MOUNT_DIRS = ("/run/netns", "/var/run/netns")


@dataclass(frozen=True)
class NamespaceEntry:
    """How to run something inside the probe's network namespace.

    ``ip -n`` and ``ip netns exec`` do more than enter a network namespace: they
    unshare the mount namespace and mount a fresh sysfs over /sys, so that
    /sys/class/net describes the namespace they entered. Where that mount is
    refused, iproute2 fails with ``mount of /sys failed: Permission denied`` and
    the probe can only answer "unknown" -- which is exactly what a node running as
    a systemd service on an SELinux host reported: ``ip`` runs there in the
    ``ifconfig_t`` domain, which is denied ``mounton`` over /sys, while the same
    probe from an interactive root shell (a different domain) went through. Being
    root is not what decides it, so root is not enough to assume it works.

    Nothing this module does inside the namespace needs sysfs: ``ip addr``/``ip
    link`` speak netlink, and the connect is a plain socket. ``nsenter --net=<netns
    path>`` enters the network namespace and touches no mounts at all, so it is
    preferred. iproute2 stays as the fallback for a host with no nsenter, where it
    is the only way in -- best effort, same as before.
    """

    exec_prefix: tuple  # prefix for an arbitrary command
    ip_prefix: tuple    # prefix for an `ip` subcommand
    name: str           # how it gets in, for the operator-facing detail


def _namespace_entry(namespace: str) -> NamespaceEntry:
    nsenter = shutil.which("nsenter")
    if nsenter:
        for directory in NETNS_MOUNT_DIRS:
            path = os.path.join(directory, namespace)
            if os.path.exists(path):
                entry = (nsenter, f"--net={path}")
                return NamespaceEntry(
                    exec_prefix=entry, ip_prefix=entry + ("ip",), name="nsenter"
                )
    return NamespaceEntry(
        exec_prefix=("ip", "netns", "exec", namespace),
        ip_prefix=("ip", "-n", namespace),
        name="ip netns exec",
    )


def _addresses_in_use(run: Runner, bridge: str) -> Set[str]:
    in_use: Set[str] = set()
    for args in (
        ["ip", "-4", "addr", "show", "dev", bridge],
        ["ip", "neigh", "show", "dev", bridge],
    ):
        proc = run(args)
        if proc.returncode != 0:
            continue
        for line in (proc.stdout or "").split():
            token = line.split("/")[0]
            try:
                ipaddress.ip_address(token)
            except ValueError:
                continue
            in_use.add(token)
    return in_use


def _pick_source_ips(subnet: str, gateway_ip: str, in_use: Set[str], count: int = 1) -> tuple:
    """``count`` free addresses in ``subnet``, plus the prefix length they share.

    From the top of the range down: the node hands guests addresses from the
    bottom, so the high end is where a transient probe collides least. That
    matters more than it looks -- a probe that lands on the address of a VM whose
    per-VM drop rules are still in the ruleset would measure nodo's own isolation
    policy and report it as a broken host.
    """
    network = ipaddress.ip_network(subnet, strict=False)
    hosts: List = list(network.hosts())
    if not hosts:
        raise RuntimeError(f"Subnet {subnet} has no usable host address.")
    picked: List[str] = []
    for candidate in reversed(hosts):
        text = str(candidate)
        if text == gateway_ip or text in in_use:
            continue
        picked.append(text)
        if len(picked) == count:
            return tuple(picked), network.prefixlen
    raise RuntimeError(
        f"No {count} free addresses available in {subnet} for the probe."
        if count > 1
        else f"No free address available in {subnet} for the probe."
    )



def probe_tcp_from_bridge(
    *,
    bridge: str,
    target_ip: str,
    port: int,
    subnet: str,
    run: Optional[Runner] = None,
    attempts: int = PROBE_ATTEMPTS,
    connect_timeout_s: float = PROBE_CONNECT_TIMEOUT_S,
    retry_delay_s: float = PROBE_RETRY_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
    provide_listener: bool = False,
) -> ProbeResult:
    """Try a TCP connect to ``target_ip:port`` from inside ``bridge``'s subnet.

    ``provide_listener`` opens a throwaway socket on ``port`` when nothing is
    listening, so the port can be checked before the gateway is up -- which is what
    port *assignment* needs. Leave it false where a real listener is expected: then
    "nothing is listening" is itself the finding.
    """
    runner = run or _default_runner

    if os.geteuid() != 0:
        return ProbeResult(None, "needs root to create a network namespace")

    link = runner(["ip", "link", "show", bridge])
    if link.returncode != 0:
        return ProbeResult(
            None,
            f"bridge {bridge} does not exist yet; it is created on the first instance launch",
        )

    if not _listener_present(port):
        if not provide_listener:
            return ProbeResult(
                None,
                f"nothing is listening on TCP {port} on this host, so a failed connect "
                "would say nothing about the firewall; start the node and check again",
            )
        with _temporary_listener(port) as held:
            if not held:
                return ProbeResult(
                    None, f"could not bind TCP {port} to probe it; something else holds it"
                )
            return probe_tcp_from_bridge(
                bridge=bridge,
                target_ip=target_ip,
                port=port,
                subnet=subnet,
                run=run,
                attempts=attempts,
                connect_timeout_s=connect_timeout_s,
                retry_delay_s=retry_delay_s,
                sleep=sleep,
            )

    try:
        (source_ip,), prefix_len = _pick_source_ips(
            subnet=subnet, gateway_ip=target_ip, in_use=_addresses_in_use(runner, bridge)
        )
    except RuntimeError as e:
        return ProbeResult(None, str(e))

    suffix = f"{random.randrange(16 ** 6):06x}"
    namespace = f"nodofw{suffix}"
    host_veth = f"nodofwa{suffix}"     # 13 chars, within the 15-char IFNAMSIZ limit
    guest_veth = f"nodofwb{suffix}"

    created_namespace = False
    created_link = False
    try:
        add_ns = runner(["ip", "netns", "add", namespace])
        if add_ns.returncode != 0:
            return ProbeResult(None, f"could not create netns: {_text(add_ns)}")
        created_namespace = True

        add_link = runner(
            ["ip", "link", "add", host_veth, "type", "veth", "peer", "name", guest_veth]
        )
        if add_link.returncode != 0:
            return ProbeResult(None, f"could not create veth pair: {_text(add_link)}")
        created_link = True

        # Resolved once the namespace exists, because nsenter needs a path to it.
        entry = _namespace_entry(namespace)

        for args in (
            ["ip", "link", "set", host_veth, "master", bridge],
            ["ip", "link", "set", host_veth, "up"],
            ["ip", "link", "set", guest_veth, "netns", namespace],
            [*entry.ip_prefix, "link", "set", "lo", "up"],
            [*entry.ip_prefix, "addr", "add", f"{source_ip}/{prefix_len}", "dev", guest_veth],
            [*entry.ip_prefix, "link", "set", guest_veth, "up"],
        ):
            proc = runner(args)
            if proc.returncode != 0:
                return ProbeResult(
                    None,
                    f"could not set up the probe interface (via {entry.name}): {_text(proc)}",
                )

        last = ""
        for attempt in range(1, max(1, attempts) + 1):
            connect = runner(
                [
                    *entry.exec_prefix,
                    sys.executable, "-c", _CONNECT_SNIPPET,
                    target_ip, str(port), str(connect_timeout_s),
                ]
            )
            last = _text(connect)
            if connect.returncode == 0:
                return ProbeResult(
                    True,
                    f"TCP connect from {source_ip} on {bridge} to {target_ip}:{port} succeeded",
                    source_ip=source_ip,
                )
            if attempt < attempts:
                sleep(retry_delay_s)

        return ProbeResult(
            False,
            f"TCP connect from {source_ip} on {bridge} to {target_ip}:{port} failed "
            f"after {attempts} attempt(s): {last}",
            source_ip=source_ip,
        )
    finally:
        # Deleting the namespace destroys the veth peer inside it, which normally
        # takes the host side with it; the explicit delete covers the case where
        # the pair was created but never moved.
        if created_namespace:
            runner(["ip", "netns", "del", namespace])
        if created_link:
            runner(["ip", "link", "del", host_veth])


# A port nothing on this host is expected to use, high enough to stay clear of the
# range the node hands out. The guest-to-guest probe owns both ends, so the number
# only has to be free inside two throwaway namespaces.
GUEST_PROBE_PORT = 47653

# The listener binds before it prints, so a process still alive after this has
# bound. A connect that fails is re-checked against the listener anyway.
LISTENER_SETTLE_S = 0.3

_LISTEN_SNIPPET = (
    "import socket,sys\n"
    "s=socket.socket()\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind((sys.argv[1], int(sys.argv[2])))\n"
    "s.listen(8)\n"
    "s.settimeout(float(sys.argv[3]))\n"
    "print('listening', flush=True)\n"
    "try:\n"
    "    while True:\n"
    "        c,_ = s.accept()\n"
    "        c.close()\n"
    "except Exception:\n"
    "    pass\n"
    "finally:\n"
    "    s.close()\n"
)


class _ProbeUnknown(Exception):
    """The probe could not be performed. Never reported as "not reachable"."""


@dataclass(frozen=True)
class _Endpoint:
    """One throwaway guest: a namespace on the bridge, wired like a real tap."""

    namespace: str
    host_veth: str
    guest_veth: str
    ip: str
    entry: NamespaceEntry


def _default_spawn(command: Sequence[str]) -> subprocess.Popen:
    return subprocess.Popen(
        list(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


def _open_endpoint(
    stack: contextlib.ExitStack,
    runner: Runner,
    *,
    bridge: str,
    tag: str,
    suffix: str,
    ip: str,
    prefix_len: int,
) -> _Endpoint:
    """Build one probe endpoint on ``bridge``, or raise ``_ProbeUnknown``.

    Wired exactly like ``_create_tap`` does it for a real guest, isolation
    included. That is not decoration: an ordinary bridge port would let the two
    endpoints switch frames tap to tap and never reach the forward hook, so the
    probe would report success on precisely the host where every real guest fails.
    A host that will not set ``isolated on`` therefore yields "unknown", not "fine".
    """
    namespace = f"nodofw{tag}{suffix}"
    host_veth = f"nodofw{tag}h{suffix}"   # 14 chars, within the 15-char IFNAMSIZ limit
    guest_veth = f"nodofw{tag}g{suffix}"

    add_ns = runner(["ip", "netns", "add", namespace])
    if add_ns.returncode != 0:
        raise _ProbeUnknown(f"could not create netns: {_text(add_ns)}")
    stack.callback(lambda: runner(["ip", "netns", "del", namespace]))

    add_link = runner(["ip", "link", "add", host_veth, "type", "veth", "peer", "name", guest_veth])
    if add_link.returncode != 0:
        raise _ProbeUnknown(f"could not create veth pair: {_text(add_link)}")
    # Deleting the namespace destroys the peer inside it, which normally takes the
    # host side with it; the explicit delete covers the case where the pair was
    # created but never moved.
    stack.callback(lambda: runner(["ip", "link", "del", host_veth]))

    # Resolved once the namespace exists, because nsenter needs a path to it.
    entry = _namespace_entry(namespace)

    for args in (
        ["ip", "link", "set", host_veth, "master", bridge],
        ["ip", "link", "set", "dev", host_veth, "type", "bridge_slave", "isolated", "on"],
        ["ip", "link", "set", host_veth, "up"],
        ["ip", "link", "set", guest_veth, "netns", namespace],
        [*entry.ip_prefix, "link", "set", "lo", "up"],
        [*entry.ip_prefix, "addr", "add", f"{ip}/{prefix_len}", "dev", guest_veth],
        [*entry.ip_prefix, "link", "set", guest_veth, "up"],
    ):
        proc = runner(args)
        if proc.returncode != 0:
            raise _ProbeUnknown(
                f"could not set up the probe interface (via {entry.name}): {_text(proc)}"
            )

    return _Endpoint(
        namespace=namespace, host_veth=host_veth, guest_veth=guest_veth, ip=ip, entry=entry
    )


def probe_tcp_between_guests(
    *,
    bridge: str,
    subnet: str,
    gateway_ip: str,
    port: int = GUEST_PROBE_PORT,
    run: Optional[Runner] = None,
    spawn: Optional[Callable[[Sequence[str]], subprocess.Popen]] = None,
    attempts: int = PROBE_ATTEMPTS,
    connect_timeout_s: float = PROBE_CONNECT_TIMEOUT_S,
    retry_delay_s: float = PROBE_RETRY_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
) -> ProbeResult:
    """Can one guest on this bridge reach another one? Tried, not assumed.

    The parent-to-child path. A service that launches a dependency talks to it over
    this exact route, and when a foreign forward chain drops it the failure arrives
    disguised: the parent sees a connect timeout, which reads from the outside like
    a child that died rather than a packet the host discarded. So the answer comes
    from sending the packet, the way ``probe_tcp_from_bridge`` does for the gateway.

    Two throwaway namespaces, both wired like a real guest -- same bridge, same
    subnet, isolated ports -- so the traffic is routed by the host and crosses the
    forward hook rather than being switched inside the bridge.

    What this does *not* measure is nodo's own per-VM policy: those rules match on a
    registered VM's address and the probe's addresses are not one, so a failure here
    is host configuration, not isolation working as intended.

    Peer reachability is pinned with an explicit host route through the gateway in
    both directions, rather than relying on the proxy-ARP half of
    ``_ensure_guest_l2_isolation``: that is re-applied on every launch and may not be
    in place when doctor runs on a stopped node, and it is not what this is asking
    about. ``net.ipv4.ip_forward`` is the one prerequisite left, and it is reported
    rather than assumed -- or set.
    """
    runner = run or _default_runner
    spawner = spawn or _default_spawn

    if os.geteuid() != 0:
        return ProbeResult(None, "needs root to create a network namespace")

    link = runner(["ip", "link", "show", bridge])
    if link.returncode != 0:
        return ProbeResult(
            None,
            f"bridge {bridge} does not exist yet; it is created on the first instance launch",
        )

    forwarding = runner(["sysctl", "-n", "net.ipv4.ip_forward"])
    if forwarding.returncode == 0 and (forwarding.stdout or "").strip() == "0":
        return ProbeResult(
            None,
            "net.ipv4.ip_forward is 0, so this host routes nothing between guests yet; "
            "the node sets it on the first instance launch",
        )

    try:
        (source_ip, peer_ip), prefix_len = _pick_source_ips(
            subnet=subnet,
            gateway_ip=gateway_ip,
            in_use=_addresses_in_use(runner, bridge),
            count=2,
        )
    except RuntimeError as e:
        return ProbeResult(None, str(e))

    suffix = f"{random.randrange(16 ** 5):05x}"
    listener: Optional[subprocess.Popen] = None

    try:
        with contextlib.ExitStack() as stack:
            client = _open_endpoint(
                stack, runner, bridge=bridge, tag="c", suffix=suffix,
                ip=source_ip, prefix_len=prefix_len,
            )
            peer = _open_endpoint(
                stack, runner, bridge=bridge, tag="p", suffix=suffix,
                ip=peer_ip, prefix_len=prefix_len,
            )

            for endpoint, other in ((client, peer), (peer, client)):
                route = runner([
                    *endpoint.entry.ip_prefix, "route", "add", f"{other.ip}/32",
                    "via", gateway_ip, "dev", endpoint.guest_veth,
                ])
                if route.returncode != 0:
                    return ProbeResult(
                        None, f"could not route {endpoint.ip} to {other.ip}: {_text(route)}"
                    )

            listener = spawner([
                *peer.entry.exec_prefix,
                sys.executable, "-c", _LISTEN_SNIPPET,
                peer.ip, str(port), str(connect_timeout_s * max(1, attempts) + 5),
            ])
            stack.callback(lambda: _stop_listener(listener))

            sleep(LISTENER_SETTLE_S)
            if listener.poll() is not None:
                return ProbeResult(
                    None,
                    f"the probe listener could not start on {peer.ip}:{port}: "
                    f"{_listener_output(listener)}",
                )

            last = ""
            for attempt in range(1, max(1, attempts) + 1):
                connect = runner([
                    *client.entry.exec_prefix,
                    sys.executable, "-c", _CONNECT_SNIPPET,
                    peer.ip, str(port), str(connect_timeout_s),
                ])
                last = _text(connect)
                if connect.returncode == 0:
                    return ProbeResult(
                        True,
                        f"TCP connect from {source_ip} to {peer_ip}:{port} across {bridge} "
                        "succeeded, so the host forwards guest to guest",
                        source_ip=source_ip,
                    )
                if attempt < attempts:
                    sleep(retry_delay_s)

            if listener.poll() is not None:
                return ProbeResult(
                    None,
                    f"the probe listener on {peer.ip}:{port} exited mid-probe, so the "
                    f"failed connect proves nothing: {_listener_output(listener)}",
                )

            return ProbeResult(
                False,
                f"TCP connect from {source_ip} to {peer_ip}:{port} across {bridge} failed "
                f"after {attempts} attempt(s): {last}",
                source_ip=source_ip,
            )
    except _ProbeUnknown as e:
        return ProbeResult(None, str(e))


def _stop_listener(listener: Optional[subprocess.Popen]) -> None:
    if listener is None or listener.poll() is not None:
        return
    listener.terminate()
    try:
        listener.wait(timeout=2)
    except Exception:
        listener.kill()


def _listener_output(listener: subprocess.Popen) -> str:
    try:
        out = (listener.communicate(timeout=2)[0] or "").strip()
    except Exception:
        out = ""
    return out or f"exit status {listener.returncode}"


def _text(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stdout or "") + (proc.stderr or "")).strip() or f"exit status {proc.returncode}"
