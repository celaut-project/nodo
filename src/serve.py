import sys
import threading
from concurrent import futures

import grpc

from protos import celaut_pb2, celaut_pb2_grpc
from src.gateway.gateway import Gateway
from src.gateway.utils import plaintext_gateway_host
from src.manager.maintain import manager_thread
from src.tunneling import delegated_endpoints
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.firewall.gateway import (
    GatewayPortUnavailable,
    defer_operator_notice,
    ensure_gateway_port_open,
    operator_notice,
)
from src.utils.firewall.legacy import sweep_compat_tables
from src.utils.firewall.reachability import ProbeResult, probe_tcp_from_bridge
from src.identity.grpc_transport import server_credentials
from src.utils.network_policy import NetworkPolicy, NetworkPolicyConfigError

env_manager = ConfigManager()


def _gateway_port_call(port: int, *, verify: bool) -> ProbeResult:
    """Apply nodo's accept rule for ``port``, optionally probing it afterwards."""
    return ensure_gateway_port_open(
        port=port,
        bridge=str(env_manager.get("virtualizers.ch.NETWORK_BRIDGE_NAME", "nodo-br-ch")),
        gateway_ip=str(env_manager.get("virtualizers.ch.NETWORK_GATEWAY_IP", "192.168.200.1")),
        subnet=str(env_manager.get("virtualizers.ch.NETWORK_SUBNET", "192.168.200.0/24")),
        verify=verify,
        strict=True,
        config_path=env_manager.config_path,
        log=log.LOGGER,
    )


def _ensure_guest_bridge() -> None:
    """Bring up the guest bridge now, rather than on the first instance launch.

    The bridge is the only place the gateway port can be probed from the way a
    guest reaches it, so creating it here is what makes the verification below
    conclusive on a node that has never run anything. Without it the probe can only
    answer "the bridge does not exist yet", which is how this node ran for two days
    with a gateway port firewalld was rejecting: the check existed, it just had
    nowhere to run from.

    Best-effort: a host where the bridge cannot be created cannot launch instances
    either, and that failure belongs to the launch path with its own diagnostics.
    Here it only costs an inconclusive probe, which is reported as such.
    """
    try:
        from src.virtualizers.microvm.network import ensure_guest_bridge

        ensure_guest_bridge()
    except Exception as e:
        log.LOGGER(
            f"Could not bring up the guest bridge before verifying the gateway port: {e}. "
            "The port cannot be proven reachable until an instance has run."
        )


def _open_gateway_port(port: int) -> None:
    """Re-open the gateway port in the host firewall, before anything binds it.

    Netfilter rules do not survive a reboot but config.yaml does, so the accept
    rule has to be re-applied on every start rather than only when the port is
    first assigned -- otherwise the first reboot silently closes the gateway for
    good.

    No probe here: nothing is listening yet, and a connect to a port with no
    listener fails whatever the firewall says (see
    ``src.utils.firewall.reachability``). Verification happens in
    ``_verify_gateway_port``, once the server is up and can answer.
    """
    try:
        _gateway_port_call(port, verify=False)
    except GatewayPortUnavailable as e:
        _refuse_to_start(e)


def _verify_gateway_port(port: int) -> None:
    """Check a guest can actually reach the gateway, now that it is listening.

    The probe is what turns a silent misconfiguration into a startup failure. This
    node ran for two days with a correct accept rule that a higher-priority
    foreign chain was rejecting: every service it accepted was unable to call back
    into the gateway, and nothing noticed, because nothing ever tried the path a
    guest takes. An unreachable gateway is worse than a stopped node, so a
    conclusive failure stops here.

    It has to run *after* ``server.start()``: the answer is only conclusive when
    something is there to answer.
    """
    if not bool(env_manager.get("network.VERIFY_GATEWAY_REACHABILITY", True)):
        return
    if env_manager.gateway_port_passed(port):
        log.LOGGER(
            f"Gateway port {port} was already proven reachable in this boot; skipping "
            "the probe."
        )
        return
    try:
        probe = _gateway_port_call(port, verify=True)
    except GatewayPortUnavailable as e:
        _refuse_to_start(e)
        return

    # Only a conclusive yes is recorded, and an inconclusive probe does not raise --
    # so "it did not fail" is not the test. A marker written on an unknown would be
    # the original bug in a new place: a port nobody proved, never checked again.
    if probe.reachable is True:
        env_manager.mark_gateway_port_passed(port)


def _verify_plaintext_gateway_port(port: int) -> None:
    """The plaintext port's counterpart of ``_verify_gateway_port`` -- never fatal.

    Guests are the only intended audience: the TLS port is what peers and the CLI
    dial, and ``network.GATEWAY_PLAINTEXT_PORT`` is not announced to either (issue
    #257 / ``docs/FIREWALL.md``). So a failure here means every service this node
    launches from now on is handed an address in its ``__config__.gateway`` that it
    cannot call back into -- resource changes, dependency launches and observation
    all go quiet -- which is worth an operator alert, but never worth refusing to
    serve: peers keep working through the TLS port regardless.

    Reuses the exact probe the TLS port uses (``probe_tcp_from_bridge``, guest
    subnet, real packet, not a read of the ruleset) because the failure mode is the
    same one: an accept rule can exist and still lose to a foreign chain on the
    same input hook. It does not, unlike the TLS path, open or withdraw any accept
    rule of its own -- the plaintext port gets no *global* rule (only the per-VM
    ones the launch path writes for the guest it hands this port to), so there is
    nothing here for nodo to open or take back.
    """
    if not port:
        return
    if not bool(env_manager.get("network.VERIFY_GATEWAY_REACHABILITY", True)):
        return
    if env_manager.plaintext_gateway_port_passed(port):
        log.LOGGER(
            f"Plaintext gateway port {port} was already proven reachable in this "
            "boot; skipping the probe."
        )
        return

    bridge = str(env_manager.get("virtualizers.ch.NETWORK_BRIDGE_NAME", "nodo-br-ch"))
    gateway_ip = str(env_manager.get("virtualizers.ch.NETWORK_GATEWAY_IP", "192.168.200.1"))
    subnet = str(env_manager.get("virtualizers.ch.NETWORK_SUBNET", "192.168.200.0/24"))
    probe = probe_tcp_from_bridge(bridge=bridge, target_ip=gateway_ip, port=port, subnet=subnet)

    if probe.reachable is True:
        env_manager.mark_plaintext_gateway_port_passed(port)
        log.LOGGER(
            f"Verified plaintext gateway port {port} is reachable from {bridge}: "
            f"{probe.detail}"
        )
        return

    if probe.reachable is None:
        log.LOGGER(
            f"Could not verify the plaintext gateway port {port} from the guest "
            f"subnet ({probe.detail}). Run 'nodo doctor' once an instance has run."
        )
        return

    lines = [
        f"The plaintext gateway port {port} is not reachable from the guest subnet "
        f"{subnet}: {probe.detail} Every service this node launches from now on is "
        "handed this address and will be unable to call back into it.",
    ]
    try:
        from src.utils.firewall.backends import detect_backend

        rejectors = detect_backend().foreign_input_rejectors()
        lines.extend(rejectors.describe())
    except Exception:
        pass
    lines.append("")
    from src.utils.firewall.frontend import detect_scoped_frontend, open_scoped_port_advice

    # Detected once, up front, so both the prose below and the bare command
    # (for `nodo info` and the TUI to show in place) come from the same result,
    # rather than shelling out to the detectors a second time for it.
    frontend = detect_scoped_frontend(port, subnet)
    lines.extend(open_scoped_port_advice(port, subnet=subnet, bridge=bridge, frontend=frontend))
    lines.append("")
    lines.append(
        "Keep the rule scoped to that subnet: this port speaks plain gRPC with no "
        "authentication, and opening it beyond the guests it is handed to would "
        "hand every one of them, and anything else on this LAN, an unauthenticated "
        "path into the node."
    )

    # emit_plaintext_gateway_notice owns the framing, the log line and the disk
    # write; logging the same block here too would put two copies of one alert in
    # app.log, which is the exact noise the framed-notice convention exists to cut.
    env_manager.emit_plaintext_gateway_notice(
        "plaintext gateway unreachable",
        "\n".join(lines),
        command=frontend.command if frontend else None,
    )


def _verify_gateway_ports(server, port: int, plaintext_port: int) -> None:
    """Probe both gateway ports from the guest subnet, once ``server`` is listening.

    An unreachable TLS port stops the node; an unreachable plaintext port never does.
    Both are probed either way, so a refusal over the TLS port still leaves the
    plaintext port's verdict (and its alert in `nodo info` / the TUI) behind.
    """
    try:
        _verify_gateway_port(port)
    except SystemExit:
        # Probe the plaintext port before going down, while its listener is still up:
        # the firewall that just blocked the TLS port almost always blocks this one
        # too, and without this its alert never reaches `nodo info` or the TUI -- the
        # operator opens the TLS port, restarts, and only then learns about the second.
        # Never allowed to replace the refusal it runs inside of.
        try:
            _verify_plaintext_gateway_port(plaintext_port)
        except Exception as e:
            log.LOGGER(f'Could not probe the plaintext gateway port {plaintext_port}: {e}')
        server.stop(0)
        raise

    # Never inside the try above: an unreachable plaintext port is an operator
    # alert, not a reason to stop what the TLS check just proved works.
    _verify_plaintext_gateway_port(plaintext_port)


def _refuse_to_start(e: GatewayPortUnavailable) -> None:
    # Framed like every other gateway-port message, so it stays readable when it
    # lands between whatever else the start path is printing.
    notice = operator_notice("refusing to start", str(e))
    log.LOGGER(notice)
    # Only the "the firewall rejects this port" refusal is persisted: it is the
    # one diagnosis `nodo serve` cannot retry on its own, so it is the one an
    # operator who is not watching this run still needs `nodo info` to surface
    # afterwards. Without this, `nodo info` cannot tell "the firewall is blocking
    # this" from "nobody has started it yet" and keeps sending the operator back
    # to the very command that just failed.
    if e.blocked_by_firewall:
        # emit_gateway_notice defers this same body itself (as well as persisting
        # it to disk for `nodo info`), so deferring `notice` too would print the
        # identical framed block twice at exit.
        env_manager.emit_gateway_notice(e.summary, str(e), command=e.command)
    else:
        # Deferred rather than printed: everything this start path has already
        # written is above it, and in a terminal the last line is the one that
        # gets read. The atexit hook fires on the SystemExit below.
        defer_operator_notice(notice)
    raise SystemExit(1) from e


def _report_network_policy() -> None:
    """Print which communication domains this node will serve, once, at start.

    Read here so a policy the node cannot parse stops it now instead of failing
    every launch later, and printed even when it restricts nothing: a control the
    operator cannot see in the log is one they cannot tell is in force (#280).
    """
    try:
        log.LOGGER(f"Network policy -- {NetworkPolicy.from_config().describe()}")
    except NetworkPolicyConfigError as e:
        notice = operator_notice("refusing to start", str(e))
        log.LOGGER(notice)
        print(notice, file=sys.stderr, flush=True)
        raise SystemExit(1) from e


def serve():
    # Resolved before anything else: a node whose gateway port is unassigned or
    # unreachable cannot serve a single request, and every service it accepted
    # would be unable to call back into it.
    #
    # Asked for explicitly. Assignment writes a firewall rule and a config value,
    # so it happens where that is the intent -- here and the installer -- rather
    # than as a side effect of loading the config, which any privileged `nodo`
    # command used to trigger.
    try:
        env_manager.assign_gateway_port_if_unset()
    except GatewayPortUnavailable as e:
        _refuse_to_start(e)
    port = env_manager.get_gateway_port()
    _ensure_guest_bridge()
    _open_gateway_port(port)

    _report_network_policy()

    # One-time migration: versions before nodo managed nftables natively wrote
    # their rules through the iptables compatibility tables. On an nftables host
    # those are now duplicates at best, and a stale DNAT pointing a published
    # port at a dead VM at worst.
    try:
        swept = sweep_compat_tables(log=log.LOGGER)
        if swept:
            log.LOGGER(f"Removed {swept} firewall rule(s) left by a pre-nftables nodo.")
    except Exception as e:
        log.LOGGER(f"Could not sweep pre-nftables firewall rules: {e}")

    # Re-open the local tunnel endpoints that delegated instances were given
    # before the last shutdown; clients hold those addresses and cannot be told
    # about new ones.
    try:
        delegated_endpoints.restore()
    except Exception as e:
        log.LOGGER(f'Could not restore delegated tunnel endpoints: {e}')

    # Run manager.
    threading.Thread(
        target=manager_thread,
        daemon=True
    ).start()

    # create a gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=30))
    celaut_pb2_grpc.add_GatewayServicer_to_server(
        Gateway(), server=server
    )

    SERVICE_NAMES = (
        celaut_pb2.DESCRIPTOR.services_by_name['Gateway'].full_name,
    )

    # The peer- and CLI-facing port is TLS (issue #257): its certificate proves this
    # node's identity key, so a caller can tell it reached us and not whoever holds the
    # address now. This is the only port announced to peers, and the only one this
    # node's own client code ever dials.
    server.add_secure_port('[::]:' + str(port), server_credentials())

    # ...and, optionally, the same servicer in plain gRPC on a second port, for the
    # services this node runs -- they speak plain gRPC and are handed this port in
    # their `__config__.gateway` -- and for external callers that do not want TLS.
    # One `Gateway()` on two ports rather than a second server: same handlers, same
    # threads, no duplicated path to keep in step. TLS is what we *offer*; requiring
    # it of a service we execute would mean shipping certificate pinning into every
    # service SDK for a hop that never leaves the host.
    #
    # It binds one address, never `[::]`: the very address the config file already
    # names as this node's gateway -- `virtualizers.ch.NETWORK_BRIDGE_NAME`, resolved
    # through the same `peer_gateway_instance` path that writes `__config__.gateway`.
    # That is the proto contract talking, so the port answers exactly where a service
    # was told to find it and nowhere else. Binding every interface would put the whole
    # unauthenticated Gateway API on any network this host can be reached from, which
    # the TLS port exists precisely to prevent; loopback is the fallback when the bridge
    # is not up yet.
    plaintext_port = env_manager.get_plaintext_gateway_port()
    if plaintext_port:
        host = plaintext_gateway_host()
        # An IPv6 literal needs brackets to be told apart from the port separator.
        address = f'[{host}]' if ':' in host else host
        bound = server.add_insecure_port(f'{address}:{plaintext_port}')
        if bound:
            log.LOGGER(
                f'Serving plain gRPC on {address}:{bound} (TLS on {port}).'
            )
        else:
            # Not fatal for peers, but every service launched from now on is handed this
            # port in its __config__ and will find nothing listening, so it must not be
            # silent.
            log.LOGGER(
                f'Could not bind the plaintext gateway port {plaintext_port} '
                f'on {host} -- services will be handed an address that answers nothing. '
                'Free that port, or point network.GATEWAY_PLAINTEXT_PORT at another one '
                '(0 makes services use the TLS port).'
            )

    server.start()

    # Only now can the guest-side probe distinguish "the firewall drops this" from
    # "nothing answers on this port".
    _verify_gateway_ports(server, port, plaintext_port)

    server.wait_for_termination()
