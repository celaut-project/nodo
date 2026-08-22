"""Policy for the one host port the node cannot work without: the gRPC gateway.

Guests reach the node at ``<guest bridge gateway>:<network.GATEWAY_PORT>``, and
every ``node_controller`` call a service makes -- launching a dependency,
modifying its own resources, observing traffic -- goes through it. If that port is
closed the node looks alive and is useless, so the rules here are strict:

* The accept rule is (re)applied on **every** daemon start, not once when the port
  is first assigned. Netfilter rules do not survive a reboot; a port stored in
  config.yaml does.
* A port is only ever persisted once it has been opened *and* proven reachable.
* ``network.GATEWAY_PORT`` never resolves to the string ``auto``. No port means a
  clear exception and a stopped process, never a plausible-looking default.

Nothing here imports ``ConfigManager``; callers pass the values in.
"""

import os
from typing import Callable, List, Optional, Sequence

from src.utils.firewall.backends import (
    FirewallBackend,
    FirewallError,
    ForeignRejector,
    Runner,
    detect_backend,
)
from src.utils.firewall.reachability import ProbeResult, probe_tcp_from_bridge

GATEWAY_COMMENT_PREFIX = "nodo;gateway;port"
# What nodo wrote before this module existed: same purpose, no port in the
# comment, so it cannot be pruned when the port changes. Cleaned up on sight.
LEGACY_GATEWAY_COMMENT = "nodo;gateway;auto_port"


def gateway_comment(port: int, protocol: str = "tcp") -> str:
    return f"{GATEWAY_COMMENT_PREFIX}={port}/{protocol}"


class GatewayPortUnavailable(Exception):
    """The gateway port is unusable, and the node must not pretend otherwise.

    Raised both when no port is assigned and when an assigned port is provably
    unreachable from the guest subnet. ``instructions`` is operator-facing text.
    """

    def __init__(self, summary: str, instructions: str = "", port: Optional[int] = None):
        self.summary = summary
        self.instructions = instructions
        self.port = port
        super().__init__(summary if not instructions else f"{summary}\n\n{instructions}")


def unassigned_port_error(config_path: str = "config.yaml") -> GatewayPortUnavailable:
    """``network.GATEWAY_PORT`` is still ``auto`` (or empty): nothing can run."""
    return GatewayPortUnavailable(
        summary=(
            "network.GATEWAY_PORT is not assigned. The node cannot serve, and no "
            "command that talks to it can run."
        ),
        instructions=(
            "Assigning a port means opening it in the host firewall, which needs root. "
            "Do one of:\n"
            "  1. Start the node as root once so it can assign and open a port:\n"
            "       sudo nodo serve\n"
            f"  2. Or pin a port you have already opened yourself, in {config_path}:\n"
            "       network:\n"
            "         GATEWAY_PORT: 58443\n"
            "     ...and open it for both the guest bridge and any external peers.\n"
            "Run 'nodo doctor' afterwards: it checks the port is genuinely reachable "
            "from the guest subnet, which a firewall rule alone does not prove."
        ),
    )


def _blocked_port_error(
    *,
    port: int,
    backend_name: str,
    subnet: str,
    bridge: str,
    probe: ProbeResult,
    rejectors: Sequence[ForeignRejector],
    config_path: str,
) -> GatewayPortUnavailable:
    lines: List[str] = [
        f"nodo added an accept rule for TCP {port} via {backend_name}, but the port is "
        f"still not reachable from the guest subnet {subnet}.",
        f"Probe: {probe.detail}",
        "",
        "An accept rule cannot fix this on its own. In nftables 'accept' ends the "
        "evaluation of its own chain only; the packet still traverses every other base "
        "chain on the same hook, and a reject there wins regardless of priority.",
    ]

    if rejectors:
        lines.append("")
        lines.append("Chains on the input hook, outside nodo's own table, that can reject:")
        for rejector in rejectors:
            lines.append(f"  - {rejector}")

    lines.append("")
    lines.append("Open the port in whatever owns that ruleset, then start the node again.")

    if any("firewalld" in rejector.table for rejector in rejectors):
        lines.extend(
            [
                "This host runs firewalld. nodo does not manage it; you need to allow both "
                "the port and the guest bridge:",
                f"  sudo firewall-cmd --permanent --add-port={port}/tcp",
                f"  sudo firewall-cmd --permanent --zone=trusted --add-interface={bridge}",
                "  sudo firewall-cmd --reload",
            ]
        )

    lines.extend(
        [
            "",
            f"Alternatively pin a port that is already open, in {config_path}:",
            "  network:",
            "    GATEWAY_PORT: <an open port>",
        ]
    )

    return GatewayPortUnavailable(
        summary=f"Gateway port {port} is not reachable from the guest subnet.",
        instructions="\n".join(lines),
        port=port,
    )


def cleanup_legacy_rules(
    backend: FirewallBackend,
    *,
    log: Callable[[str], None] = lambda message: None,
) -> None:
    """Remove pre-existing ``nodo;gateway;auto_port`` rules, whatever the backend.

    Old nodo versions wrote these through ``iptables``, so they can be sitting in
    the compatibility table while we now manage nftables natively. Best-effort:
    an orphan accept rule is untidy, not dangerous.
    """
    from src.utils.firewall.backends import IptablesBackend

    candidates: List[FirewallBackend] = [backend]
    if not isinstance(backend, IptablesBackend):
        try:
            candidates.append(IptablesBackend(run=backend._run))
        except Exception:
            pass

    for candidate in candidates:
        try:
            for rule in candidate.list_input_accepts(LEGACY_GATEWAY_COMMENT):
                candidate.remove_input_accept(rule)
                log(f"[FW] Removed legacy gateway rule ({candidate.name}): {rule.comment}")
        except FirewallError as e:
            log(f"[FW] Could not clean up legacy gateway rules ({candidate.name}): {e}")
        except Exception:
            continue


def ensure_gateway_port_open(
    port: int,
    *,
    bridge: Optional[str] = None,
    gateway_ip: Optional[str] = None,
    subnet: Optional[str] = None,
    backend: Optional[FirewallBackend] = None,
    verify: bool = True,
    strict: bool = True,
    config_path: str = "config.yaml",
    log: Callable[[str], None] = lambda message: None,
    run: Optional[Runner] = None,
) -> ProbeResult:
    """Open ``port`` on the input hook and, when asked, prove it is reachable.

    Idempotent, so it belongs in the daemon's start path. Raises
    ``GatewayPortUnavailable`` when the port is provably unreachable and
    ``strict``; an *inconclusive* probe only warns, because "the guest bridge does
    not exist yet" is the normal state of a node that has never run an instance.
    """
    if os.geteuid() != 0:
        raise GatewayPortUnavailable(
            summary=f"Opening the gateway port {port} needs root.",
            instructions=(
                "Start the node with root privileges (e.g. 'sudo nodo serve'), or open "
                f"TCP {port} yourself before starting it."
            ),
            port=port,
        )

    active = backend or detect_backend(run=run)
    comment = gateway_comment(port)

    try:
        added = active.ensure_input_accept(port=port, protocol="tcp", comment=comment)
    except FirewallError as e:
        raise GatewayPortUnavailable(
            summary=f"Could not open the gateway port {port} with {active.name}.",
            instructions=(
                f"{e}\n\nOpen TCP {port} manually and pin it in {config_path} as "
                "network.GATEWAY_PORT, or fix the firewall tooling on this host."
            ),
            port=port,
        ) from e

    if added:
        log(f"[FW] Opened gateway port {port}/tcp via {active.name}.")
    for removed in active.prune_input_accepts(GATEWAY_COMMENT_PREFIX, keep=comment):
        log(f"[FW] Removed stale gateway rule via {active.name}: {removed.comment}")
    cleanup_legacy_rules(active, log=log)

    if not verify:
        return ProbeResult(None, "verification not requested")

    if not (bridge and gateway_ip and subnet):
        return ProbeResult(None, "guest network is not configured; nothing to probe from")

    probe = probe_tcp_from_bridge(
        bridge=bridge, target_ip=gateway_ip, port=port, subnet=subnet, run=run
    )

    if probe.reachable is True:
        log(f"[FW] Verified gateway port {port} is reachable from {bridge}: {probe.detail}")
        return probe

    if probe.reachable is None:
        log(
            f"[FW] Could not verify the gateway port {port} from the guest subnet "
            f"({probe.detail}). Run 'nodo doctor' once an instance has run."
        )
        return probe

    rejectors = _safe_rejectors(active)
    error = _blocked_port_error(
        port=port,
        backend_name=active.name,
        subnet=subnet,
        bridge=bridge,
        probe=probe,
        rejectors=rejectors,
        config_path=config_path,
    )
    if strict:
        raise error
    log(f"[FW] {error}")
    return probe


def _safe_rejectors(backend: FirewallBackend) -> List[ForeignRejector]:
    try:
        return backend.foreign_input_rejectors()
    except Exception:
        return []
