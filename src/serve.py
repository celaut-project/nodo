import sys
import threading
from concurrent import futures

import grpc

from protos import celaut_pb2, celaut_pb2_grpc
from src.gateway.gateway import Gateway
from src.manager.maintain import manager_thread
from src.tunneling import delegated_endpoints
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.firewall.gateway import (
    GatewayPortUnavailable,
    ensure_gateway_port_open,
    operator_notice,
)
from src.utils.firewall.legacy import sweep_compat_tables

env_manager = ConfigManager()


def _gateway_port_call(port: int, *, verify: bool) -> None:
    """Apply nodo's accept rule for ``port``, optionally probing it afterwards."""
    ensure_gateway_port_open(
        port=port,
        bridge=str(env_manager.get("virtualizers.ch.NETWORK_BRIDGE_NAME", "br-ch")),
        gateway_ip=str(env_manager.get("virtualizers.ch.NETWORK_GATEWAY_IP", "192.168.200.1")),
        subnet=str(env_manager.get("virtualizers.ch.NETWORK_SUBNET", "192.168.200.0/24")),
        verify=verify,
        strict=True,
        config_path=env_manager.config_path,
        log=log.LOGGER,
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
    try:
        _gateway_port_call(port, verify=True)
    except GatewayPortUnavailable as e:
        _refuse_to_start(e)


def _refuse_to_start(e: GatewayPortUnavailable) -> None:
    # Framed like every other gateway-port message, so it stays readable when it
    # lands between whatever else the start path is printing.
    notice = operator_notice("refusing to start", str(e))
    log.LOGGER(notice)
    print(notice, file=sys.stderr, flush=True)
    raise SystemExit(1) from e


def serve():
    # Resolved before anything else: a node whose gateway port is unassigned or
    # unreachable cannot serve a single request, and every service it accepted
    # would be unable to call back into it.
    port = env_manager.get_gateway_port()
    _open_gateway_port(port)

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

    server.add_insecure_port('[::]:' + str(port))

    server.start()

    # Only now can the guest-side probe distinguish "the firewall drops this" from
    # "nothing answers on this port".
    try:
        _verify_gateway_port(port)
    except SystemExit:
        server.stop(0)
        raise

    server.wait_for_termination()
