"""Policy for the one host port the node cannot work without: the gRPC gateway.

Guests reach the node at ``<guest bridge gateway>:<network.GATEWAY_PORT>``, and
every ``node_controller`` call a service makes -- launching a dependency,
modifying its own resources, observing traffic -- goes through it. If that port is
closed the node looks alive and is useless, so the rules here are strict:

* The accept rule is (re)applied on **every** daemon start, not once when the port
  is first assigned. Netfilter rules do not survive a reboot; a port stored in
  config.yaml does.
* Reachability is proven **once per port per boot**, in the daemon's start path,
  and the node refuses to serve on a port that is provably unreachable. The port
  is stored before that happens, on purpose: verifying at assignment time meant a
  host whose guest bridge did not exist yet could never assign anything, which
  left pinning a port by hand -- the one path with no check on it -- as the only
  way forward. The verdict is cached in ``<CACHE>/gateway_port_passed``.
* A path that opens a port and then refuses to use it takes its rule back out.
  An accept rule for a port nothing will ever answer on is a hole, not a leftover.
* ``network.GATEWAY_PORT`` never resolves to the string ``auto``. No port means a
  clear exception and a stopped process, never a plausible-looking default.

* A refusal ends with the single command that fixes it, when
  ``firewall.frontend`` could detect which front-end is running here. nodo still
  drives none of them -- it prints the command, the operator runs it.

Nothing here imports ``ConfigManager``; callers pass the values in.
"""

import atexit
import os
import sys
import textwrap
from typing import Callable, List, Optional

from src.utils.firewall.backends import (
    FirewallBackend,
    FirewallError,
    RejectorScan,
    Runner,
    detect_backend,
)
from src.utils.firewall.frontend import open_port_advice
from src.utils.firewall.rules import Chain
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


_DEFERRED_NOTICES: List[str] = []
_FLUSH_REGISTERED = False


def defer_operator_notice(notice: str) -> None:
    """Hold ``notice`` back until this process is about to exit.

    In a terminal the last thing printed is the first thing read, and these alerts
    are emitted at the worst possible moment for that: while ConfigManager loads,
    which on a fresh install is in the middle of nodo.py's imports, with the KyA
    banner and everything else still to come. Printed in place, the one message the
    operator has to act on ends up in the middle of the scrollback.

    Registered with ``atexit``, so it survives the ``SystemExit`` that a refusal to
    start raises. Duplicates are dropped: the same notice deferred twice in one
    process is one alert, not two.
    """
    global _FLUSH_REGISTERED
    if notice in _DEFERRED_NOTICES:
        return
    _DEFERRED_NOTICES.append(notice)
    if not _FLUSH_REGISTERED:
        atexit.register(flush_operator_notices)
        _FLUSH_REGISTERED = True


def drain_operator_notices() -> List[str]:
    """Take the held-back notices, emptying the queue.

    For a caller that is going to surface them some other way: the installer prints
    ``.gateway_notice`` as its own last act, so the helper that triggered the alert
    must not also print it -- that would put the same alert in the middle of the
    install output as well as at the end, which is the problem being solved.
    """
    taken = list(_DEFERRED_NOTICES)
    _DEFERRED_NOTICES.clear()
    return taken


def flush_operator_notices() -> None:
    """Print the held-back notices to stderr, once. Also callable by hand."""
    for notice in drain_operator_notices():
        print(notice, file=sys.stderr, flush=True)


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
    rejectors: RejectorScan,
    config_path: str,
    run: Optional[Runner] = None,
) -> GatewayPortUnavailable:
    lines: List[str] = _para(
        f"nodo's accept rule for TCP {port} is in place ({backend_name}), but the port "
        f"is still not reachable from the guest subnet {subnet}: {probe.detail} "
        "Something else on the input hook rejects it, and an accept of nodo's cannot "
        "override that: in nftables 'accept' ends the evaluation of its own chain only."
    )
    lines.extend(rejectors.describe())
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


def withdraw_gateway_port(
    port: int,
    *,
    backend: Optional[FirewallBackend] = None,
    log: Callable[[str], None] = lambda message: None,
    run: Optional[Runner] = None,
) -> bool:
    """Take nodo's accept rule for ``port`` back out. True when something was removed.

    The counterpart to opening one: any path that opens a port and then decides not
    to use it has to undo that, or a refusal leaves the host with a hole for a port
    nothing will ever answer on. Best-effort -- a failure here is untidy, and must
    not replace the error that caused the withdrawal.
    """
    try:
        active = backend or detect_backend(run=run)
        removed = active.delete_by_comment(Chain.INPUT, gateway_comment(port))
    except Exception as e:
        log(f"[FW] Could not withdraw the accept rule for TCP {port}: {e}")
        return False
    if removed:
        log(f"[FW] Withdrew nodo's accept rule for TCP {port} ({active.name}).")
    return bool(removed)


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

    The probe is not asked to supply a listener: the only caller that verifies is
    the daemon, after ``server.start()``, so the gateway is already answering. A
    check on a *stopped* node is ``nodo doctor``'s job, and it drives
    ``probe_tcp_from_bridge`` directly with ``provide_listener``.
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
        # Nothing is going to serve on this port now, so nodo's accept rule for it
        # comes back out. It is re-applied by the next start, once the operator has
        # opened the port where their firewall is actually managed.
        withdraw_gateway_port(port, backend=active, log=log, run=run)
        raise error
    log(f"[FW] {error}")
    return probe


def assign_gateway_port(
    port: int,
    *,
    config_path: str = "config.yaml",
    log: Callable[[str], None] = lambda message: None,
    run: Optional[Runner] = None,
) -> None:
    """Claim ``port`` as THE gateway port: open it in nodo's ruleset, nothing more.

    Deliberately does not probe. Assignment happens while the config loads, which
    is before the guest bridge exists on a fresh host -- and a probe that cannot
    run is not a verdict. The previous version treated it as one: an inconclusive
    probe plus any foreign chain that could reject meant *refuse*, so a host with
    firewalld and no bridge yet could never be assigned a port at all, and the
    operator's only remaining move was to pin one by hand, unverified, which is how
    a node ends up serving on a port firewalld rejects.

    Reachability is now proven once per boot in the start path, where the bridge
    exists and where a negative answer can stop the node instead of just declining
    to write a number. See ``ensure_gateway_port_open``.

    Raises ``GatewayPortUnavailable`` if the rule cannot be applied at all; the
    caller must then leave the port unassigned.
    """
    ensure_gateway_port_open(
        port=port,
        verify=False,
        config_path=config_path,
        log=log,
        run=run,
    )


def _safe_rejectors(backend: FirewallBackend) -> RejectorScan:
    """The input-hook scan, with a raised exception folded into "could not read".

    Never an empty result on failure: that reads as "the hook is clear", which is
    the opposite of what happened.
    """
    try:
        return backend.foreign_input_rejectors()
    except Exception as e:
        return RejectorScan(readable=False, reason=f"reading the ruleset raised {e!r}")
