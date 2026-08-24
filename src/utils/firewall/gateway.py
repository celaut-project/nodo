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

* A refusal ends with the single command that fixes it, when
  ``firewall.frontend`` could detect which front-end is running here. nodo still
  drives none of them -- it prints the command, the operator runs it.

Nothing here imports ``ConfigManager``; callers pass the values in.
"""

import os
import textwrap
from typing import Callable, List, Optional, Sequence

from src.utils.firewall.backends import (
    FirewallBackend,
    FirewallError,
    ForeignRejector,
    Runner,
    detect_backend,
)
from src.utils.firewall.frontend import open_port_advice
from src.utils.firewall.reachability import ProbeResult, probe_tcp_from_bridge

GATEWAY_COMMENT_PREFIX = "nodo;gateway;port"
# What nodo wrote before this module existed: same purpose, no port in the
# comment, so it cannot be pruned when the port changes. Cleaned up on sight.
LEGACY_GATEWAY_COMMENT = "nodo;gateway;auto_port"


# Wide enough for a wrapped paragraph, narrow enough for an 80-column terminal.
NOTICE_RULE = "-" * 78


def operator_notice(title: str, body: str) -> str:
    """Frame ``body`` so it survives landing in the middle of other output.

    These messages are long, and the worst of them are emitted while ConfigManager
    is loading -- which on a fresh install happens during ``nodo.py``'s imports, so
    the next thing on the terminal is the KyA banner. Run together they read as one
    wall of text and the instructions get lost. Blank lines top and bottom, and a
    rule with a title, keep the block separate from whatever printed around it.
    """
    return "\n".join(["", NOTICE_RULE, f" nodo: {title}", NOTICE_RULE, body, NOTICE_RULE, ""])


def _para(text: str) -> List[str]:
    """One prose paragraph, wrapped. These messages get read in a terminal and
    pasted elsewhere, so they are wrapped here rather than left to whatever is
    displaying them."""
    return textwrap.wrap(text, width=78)


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
    run: Optional[Runner] = None,
) -> GatewayPortUnavailable:
    lines: List[str] = _para(
        f"nodo's accept rule for TCP {port} is in place ({backend_name}), but the port "
        f"is still not reachable from the guest subnet {subnet}: {probe.detail} "
        "Something else on the input hook rejects it, and an accept of nodo's cannot "
        "override that: in nftables 'accept' ends the evaluation of its own chain only."
    )
    if rejectors:
        lines.append("Rejecting chains, outside nodo's own ruleset:")
        lines.extend(f"  - {rejector}" for rejector in rejectors)
    lines.append("")
    lines.extend(open_port_advice(port, bridge=bridge, subnet=subnet, run=run))
    lines.append("")
    lines.extend(
        _para(
            f"Then start the node again -- or set network.GATEWAY_PORT in {config_path} "
            "to a port you have already opened."
        )
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
        run=run,
    )
    if strict:
        raise error
    log(f"[FW] {error}")
    return probe


def _unverified_port_error(
    *,
    port: int,
    probe: ProbeResult,
    rejectors: Sequence[ForeignRejector],
    bridge: str,
    subnet: str,
    config_path: str,
    run: Optional[Runner] = None,
) -> GatewayPortUnavailable:
    """The port could not be proven reachable AND something else can reject it."""
    lines = _para(
        f"nodo opened TCP {port} in its own ruleset but could not prove it is reachable "
        f"({probe.detail}), and this host has rules that can reject it:"
    )
    lines.extend(f"  - {rejector}" for rejector in rejectors)
    lines.extend(
        _para(
            "An accept of nodo's does not overrule them (in nftables 'accept' ends its "
            "own chain only), so rather than store a port peers may not reach, "
            "network.GATEWAY_PORT was left unassigned."
        )
    )
    lines.append("")
    lines.extend(open_port_advice(port, bridge=bridge, subnet=subnet, run=run))
    lines.append("")
    lines.extend(
        _para(
            f"Then start the node again -- or set network.GATEWAY_PORT in {config_path} "
            "to a port you have already opened. Reachability from OUTSIDE this LAN is a "
            "separate question no check here can answer: run 'nodo nat-guide' for that."
        )
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
            run=run,
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
