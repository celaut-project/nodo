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
    lines.extend(_hook_contract(port, bridge, subnet))

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
    probe_with_listener: bool = False,
    config_path: str = "config.yaml",
    log: Callable[[str], None] = lambda message: None,
    run: Optional[Runner] = None,
) -> ProbeResult:
    """Open ``port`` on the input hook and, when asked, prove it is reachable.

    Idempotent, so it belongs in the daemon's start path. Raises
    ``GatewayPortUnavailable`` when the port is provably unreachable and
    ``strict``; an *inconclusive* probe only warns, because "the guest bridge does
    not exist yet" is the normal state of a node that has never run an instance.

    ``probe_with_listener`` lets the probe supply its own throwaway listener, so
    the port can be checked before the gateway is up. Port assignment needs that;
    the daemon start path does not, because by then the gateway is listening.
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
        bridge=bridge,
        target_ip=gateway_ip,
        port=port,
        subnet=subnet,
        run=run,
        provide_listener=probe_with_listener,
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


def _hook_contract(port: int, bridge: str, subnet: str) -> List[str]:
    """A formal statement of what the host must satisfy, with no tool named.

    nodo speaks the two interfaces the kernel offers, nftables and iptables, and
    nothing above them. Which program owns the rest of the ruleset -- a distro
    front-end, a config-management template, a hand-written nft file -- is not
    nodo's to know, let alone to write to. Naming one would also be a lie of
    omission on every host that uses a different one.

    So the node states the property that has to hold, in terms of the ruleset it can
    actually read, and leaves the operator to establish it however their host is
    managed. They know what owns their firewall; nodo does not.
    """
    return [
        f"For this node to work, TCP {port} inbound must be accepted on the input",
        "hook, and no other base chain on that hook may reject or drop it:",
        f"  - from {subnet}, the guest subnet, reached over {bridge}: every",
        "    node_controller call a service makes goes this way, so without it the",
        "    node accepts services that cannot call back into it.",
        "  - from wherever peers reach this host: without it the node is invisible to",
        "    the network while looking healthy locally.",
        "",
        "How to establish that depends on what manages the ruleset on this host, which",
        "nodo deliberately does not assume: it writes nftables (or iptables) rules and",
        "reads the ruleset back, and does not drive any firewall front-end. Apply the",
        "change wherever the chains listed above are managed, then start the node again.",
    ]


def _unverified_port_error(
    *,
    port: int,
    probe: ProbeResult,
    rejectors: Sequence[ForeignRejector],
    bridge: str,
    subnet: str,
    config_path: str,
) -> GatewayPortUnavailable:
    """The port could not be proven reachable AND something else can reject it."""
    lines = [
        f"nodo opened TCP {port} in its own ruleset, but could not prove the port is "
        "actually reachable, and this host has other firewall rules that can reject it.",
        f"Probe: {probe.detail}",
        "",
        "Chains on the input hook, outside nodo's own table, that can reject:",
    ]
    lines.extend(f"  - {rejector}" for rejector in rejectors)
    lines.extend(
        [
            "",
            "An accept rule of nodo's does not overrule them: in nftables 'accept' ends "
            "the evaluation of its own chain only, and a reject in another base chain on "
            "the same hook wins regardless of priority.",
            "",
            "Rather than store a port that peers may not be able to reach, nodo left "
            "network.GATEWAY_PORT unassigned.",
            "",
            *_hook_contract(port, bridge, subnet),
            "",
            f"Or pin a port you have already opened, in {config_path}:",
            "  network:",
            "    GATEWAY_PORT: <an open port>",
            "",
            "Note that reachability from OUTSIDE this LAN is a separate question that "
            "nothing on this host can answer: run 'nodo nat-guide' for that.",
        ]
    )
    return GatewayPortUnavailable(
        summary=f"Gateway port {port} could not be verified as reachable.",
        instructions="\n".join(lines),
        port=port,
    )


def assign_gateway_port(
    port: int,
    *,
    bridge: str,
    gateway_ip: str,
    subnet: str,
    config_path: str = "config.yaml",
    log: Callable[[str], None] = lambda message: None,
    run: Optional[Runner] = None,
) -> ProbeResult:
    """Clear ``port`` for use as THE gateway port, or raise rather than settle for it.

    Assignment is stricter than the daemon's start path, because the port it picks
    gets written to config.yaml and every peer is then told to use it. A port that
    turns out to be blocked is not a transient failure there: it is a node that
    looks alive and cannot be called, which is worse than a node that refused to
    start. So:

    * The port is probed for real, with a throwaway listener, instead of being
      assumed reachable because a rule was accepted. Nothing is listening at
      assignment time -- that is precisely why this used to skip verification and
      persist a port no packet had ever traversed.
    * An INCONCLUSIVE probe is not good enough either, if the host has foreign
      chains that can reject. That combination is how a Fedora host ends up with an
      assigned port that firewalld quietly rejects, and there is no reason to
      commit to the port when the one thing that could have cleared it did not run.
    * An inconclusive probe with nothing that can reject is fine: a fresh node whose
      guest bridge does not exist yet is the ordinary case, and nodo's own accept
      rule is then the only verdict on the hook.

    Raises ``GatewayPortUnavailable`` in either failing case; the caller must leave
    the port unassigned and surface the instructions.
    """
    probe = ensure_gateway_port_open(
        port=port,
        bridge=bridge,
        gateway_ip=gateway_ip,
        subnet=subnet,
        verify=True,
        strict=True,
        probe_with_listener=True,
        config_path=config_path,
        log=log,
        run=run,
    )

    if probe.reachable is True:
        return probe

    rejectors = _safe_rejectors(detect_backend(run=run))
    if rejectors:
        raise _unverified_port_error(
            port=port,
            probe=probe,
            rejectors=rejectors,
            bridge=bridge,
            subnet=subnet,
            config_path=config_path,
        )

    log(
        f"[FW] Gateway port {port} could not be verified ({probe.detail}), but nothing "
        "outside nodo's ruleset can reject it on the input hook. Assigning it."
    )
    return probe


def _safe_rejectors(backend: FirewallBackend) -> List[ForeignRejector]:
    try:
        return backend.foreign_input_rejectors()
    except Exception:
        return []
