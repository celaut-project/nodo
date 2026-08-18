"""``nodo nat-guide`` — what to change on the router so this node is reachable.

Service tunneling means a single port has to be reachable from the Internet instead
of one per service, and DDNS keeps a name pointing at the node. Neither does the
last step: the router still has to forward that port inward. This prints the
instructions for *this* machine, with its own addresses and port filled in.

Design
------
Detection and text are kept apart, the same split ``src/commands/observe.py``
documents: :func:`collect_facts` touches the host (sockets, ``ip route``, config,
DNS) and :func:`render_guide` is a pure function of those facts. So the wording is
testable without root, without a router and without a network, and a fact that
could not be detected is *omitted* rather than guessed at.
"""

import re
import shutil
import socket
import subprocess
from typing import Dict, List, Optional

from src.utils.config import ConfigManager
from src.utils.network import resolve_public_port

env_manager = ConfigManager()

# `ip route show default` prints e.g.
#   default via 192.168.1.1 dev wlp3s0 proto dhcp src 192.168.1.34 metric 600
_DEFAULT_ROUTE_GATEWAY = re.compile(r"^default\s+via\s+(\S+)")

PROBE_TIMEOUT_S = 1.0


def parse_default_gateway(ip_route_output: str) -> Optional[str]:
    """Pull the router address out of ``ip route show default`` output.

    Returns None for anything unexpected — a missing default route, an IPv6-only
    answer we do not handle, or a different tool's output entirely.
    """
    for line in (ip_route_output or "").splitlines():
        match = _DEFAULT_ROUTE_GATEWAY.match(line.strip())
        if match:
            return match.group(1)
    return None


def _detect_default_gateway() -> Optional[str]:
    if not shutil.which("ip"):
        return None
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_default_gateway(result.stdout)


def _detect_local_ip() -> Optional[str]:
    try:
        from src.utils.network import get_local_ip

        return get_local_ip()
    except Exception:
        return None


def _gateway_port_is_listening(port: int) -> Optional[bool]:
    """Whether something answers on the gateway port locally.

    None when it cannot be determined. A local connect says nothing about whether
    the port is reachable from outside — that is the whole point of the guide.
    """
    if not port:
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(PROBE_TIMEOUT_S)
            return probe.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return None


def collect_facts() -> Dict[str, object]:
    """Everything the guide needs from this host. Undetected values stay None."""
    from src.manager.ddns import status as ddns_status

    try:
        port = int(env_manager.get("GATEWAY_PORT") or 0)
    except (TypeError, ValueError):
        port = 0

    try:
        ddns = ddns_status()
    except Exception:
        ddns = {}

    public_tcp_port = resolve_public_port(env_manager.get("network.PUBLIC_TCP_PORT", ""), port) if port else None

    return {
        "gateway_port": port or None,
        "public_tcp_port": public_tcp_port,
        "local_ip": _detect_local_ip(),
        "router_ip": _detect_default_gateway(),
        "listening": _gateway_port_is_listening(port),
        "ddns_enabled": bool(ddns.get("enabled")),
        "ddns_hostname": ddns.get("hostname"),
        "ddns_resolves_to": ddns.get("resolves_to"),
        "direct_exposure": not bool(env_manager.get("network.DISABLE_EXPOSE_OUTSIDE", False)),
        "free_ports_range": env_manager.get("network.FREE_PORTS_RANGE", []) or [],
    }


def _format_port_ranges(ranges: List[dict]) -> str:
    parts = []
    for entry in ranges:
        try:
            parts.append(f"{int(entry['START'])}-{int(entry['END'])}")
        except (KeyError, TypeError, ValueError):
            continue
    return ", ".join(parts)


def render_guide(facts: Dict[str, object]) -> str:
    """Compose the guide from ``facts``. Pure: no host access, no network."""
    port = facts.get("gateway_port")
    public_port = facts.get("public_tcp_port")
    ports_differ = bool(port and public_port and public_port != port)
    local_ip = facts.get("local_ip")
    router_ip = facts.get("router_ip")
    lines: List[str] = []

    lines.append("Making this node reachable from the Internet")
    lines.append("=" * 44)
    lines.append("")

    lines.append("What this node needs:")
    if port and ports_differ:
        lines.append(
            f"  * Inbound TCP on external port {public_port}, forwarded to this "
            f"host's gateway port {port} (set via network.PUBLIC_TCP_PORT)."
        )
    elif port:
        lines.append(f"  * Inbound TCP on port {port} (the gateway port).")
    else:
        lines.append(
            "  * Inbound TCP on the gateway port — but network.GATEWAY_PORT is not "
            "resolvable, so configure it first."
        )
    lines.append(
        "  * Service tunneling (Gateway.ServiceTunnel) carries every service through "
        "that one port, so you do NOT need a port per service."
    )
    if facts.get("direct_exposure") and facts.get("free_ports_range"):
        published = _format_port_ranges(facts["free_ports_range"])  # type: ignore[arg-type]
        if published:
            lines.append(
                f"  * Only if you also want direct exposure (services published on their "
                f"own ports): forward {published} as well. Set "
                f"network.DISABLE_EXPOSE_OUTSIDE to rely on the tunnel alone."
            )
    lines.append("")

    lines.append("This machine:")
    lines.append(f"  Local address:  {local_ip or 'could not detect'}")
    lines.append(f"  Gateway port:   {port or 'not resolvable'}")
    if ports_differ:
        lines.append(f"  Public TCP port: {public_port} (network.PUBLIC_TCP_PORT)")
    lines.append(f"  Router:         {router_ip or 'could not detect a default gateway'}")

    listening = facts.get("listening")
    if listening is True:
        lines.append("  Gateway:        listening locally [OK]")
    elif listening is False:
        lines.append(
            "  Gateway:        nothing listening locally — start the node "
            "(`nodo daemon start`) before testing from outside"
        )
    lines.append("")

    lines.append("On your router:")
    if router_ip:
        lines.append(f"  1. Open its admin page, usually http://{router_ip}/")
    else:
        lines.append("  1. Open your router's admin page.")
    lines.append(
        "  2. Find NAT / Port forwarding / Virtual servers (the name varies by vendor)."
    )
    lines.append("  3. Add a rule:")
    lines.append("       Protocol:      TCP")
    lines.append(f"       External port: {public_port if public_port else (port if port else '<gateway port>')}")
    lines.append(f"       Internal host: {local_ip or '<this machine>'}")
    lines.append(f"       Internal port: {port if port else '<gateway port>'}")
    lines.append(
        "  4. Give this machine a fixed address (DHCP reservation or a static IP), or "
        "the rule will point at the wrong host after a reboot."
    )
    lines.append("")

    if facts.get("ddns_enabled"):
        hostname = facts.get("ddns_hostname") or "not set"
        lines.append("DNS:")
        lines.append(f"  DDNS is enabled for {hostname}.")
        resolves = facts.get("ddns_resolves_to")
        if resolves:
            lines.append(f"  It currently resolves to {resolves}.")
            lines.append(
                f"  If that is not your public address, the record is stale or the "
                f"provider saw a different source address."
            )
        else:
            lines.append(
                "  It does not resolve yet — check ddns.DOMAIN and ddns.TOKEN, and the "
                "node's log for [DDNS] lines."
            )
    else:
        lines.append("DNS:")
        lines.append(
            "  DDNS is disabled. Peers will have to reach a bare IP, which changes on "
            "most home connections. See the ddns.* settings in config.yaml."
        )
    lines.append("")

    lines.append("Checking it worked:")
    target = facts.get("ddns_hostname") if facts.get("ddns_enabled") else "<your public IP>"
    check_port = public_port if public_port else (port or '<gateway port>')
    lines.append(
        f"  From OUTSIDE your network (mobile data, a remote host):\n"
        f"      nc -vz {target or '<your public IP>'} {check_port}"
    )
    lines.append(
        "  Testing from inside your own network usually succeeds regardless of the "
        "forwarding rule, so it proves nothing."
    )
    lines.append("")

    lines.append("If it still fails:")
    lines.append(
        "  * Carrier-grade NAT (CGNAT): your public address is shared and no rule on "
        "your router can help. Check whether your ISP offers a public IPv4, or reach "
        "this node outbound-only (it can still call peers and delegate work)."
    )
    lines.append("  * A second router or an ISP modem in front: the rule is needed on both.")
    lines.append("  * A host firewall: allow inbound TCP on the gateway port.")

    return "\n".join(lines)


def nat_guide() -> None:
    """Print the router/NAT guide for this host."""
    print(render_guide(collect_facts()), flush=True)
