"""Prove a host port is reachable from the guest subnet, instead of assuming it.

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
"""

import ipaddress
import os
import random
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


def _pick_source_ip(subnet: str, gateway_ip: str, in_use: Set[str]) -> tuple:
    network = ipaddress.ip_network(subnet, strict=False)
    hosts: List = list(network.hosts())
    if not hosts:
        raise RuntimeError(f"Subnet {subnet} has no usable host address.")
    # From the top of the range down: the node hands guests addresses from the
    # bottom, so the high end is where a transient probe collides least.
    for candidate in reversed(hosts):
        text = str(candidate)
        if text == gateway_ip or text in in_use:
            continue
        return text, network.prefixlen
    raise RuntimeError(f"No free address available in {subnet} for the probe.")


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
) -> ProbeResult:
    """Try a TCP connect to ``target_ip:port`` from inside ``bridge``'s subnet."""
    runner = run or _default_runner

    if os.geteuid() != 0:
        return ProbeResult(None, "needs root to create a network namespace")

    link = runner(["ip", "link", "show", bridge])
    if link.returncode != 0:
        return ProbeResult(
            None,
            f"bridge {bridge} does not exist yet; it is created on the first instance launch",
        )

    try:
        source_ip, prefix_len = _pick_source_ip(
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

        for args in (
            ["ip", "link", "set", host_veth, "master", bridge],
            ["ip", "link", "set", host_veth, "up"],
            ["ip", "link", "set", guest_veth, "netns", namespace],
            ["ip", "-n", namespace, "link", "set", "lo", "up"],
            ["ip", "-n", namespace, "addr", "add", f"{source_ip}/{prefix_len}", "dev", guest_veth],
            ["ip", "-n", namespace, "link", "set", guest_veth, "up"],
        ):
            proc = runner(args)
            if proc.returncode != 0:
                return ProbeResult(None, f"could not set up the probe interface: {_text(proc)}")

        last = ""
        for attempt in range(1, max(1, attempts) + 1):
            connect = runner(
                [
                    "ip", "netns", "exec", namespace,
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


def _text(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stdout or "") + (proc.stderr or "")).strip() or f"exit status {proc.returncode}"
