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
from src.utils.firewall.gateway import GatewayPortUnavailable, ensure_gateway_port_open

env_manager = ConfigManager()


def _open_and_verify_gateway_port(port: int) -> None:
    """Re-open the gateway port, and check a guest can actually reach it.

    Netfilter rules do not survive a reboot but config.yaml does, so the accept
    rule has to be re-applied on every start rather than only when the port is
    first assigned -- otherwise the first reboot silently closes the gateway for
    good.

    The probe is what turns a silent misconfiguration into a startup failure. This
    node ran for two days with a correct accept rule that a higher-priority
    foreign chain was rejecting: every service it accepted was unable to call back
    into the gateway, and nothing noticed, because nothing ever tried the path a
    guest takes. An unreachable gateway is worse than a stopped node, so a
    conclusive failure stops here.
    """
    verify = env_manager.get("network.VERIFY_GATEWAY_REACHABILITY", True)
    try:
        ensure_gateway_port_open(
            port=port,
            bridge=str(env_manager.get("virtualizers.ch.NETWORK_BRIDGE_NAME", "br-ch")),
            gateway_ip=str(env_manager.get("virtualizers.ch.NETWORK_GATEWAY_IP", "192.168.200.1")),
            subnet=str(env_manager.get("virtualizers.ch.NETWORK_SUBNET", "192.168.200.0/24")),
            verify=bool(verify),
            strict=True,
            config_path=env_manager.config_path,
            log=log.LOGGER,
        )
    except GatewayPortUnavailable as e:
        log.LOGGER(f"Refusing to start: {e}")
        print(f"\n{e}\n", file=sys.stderr, flush=True)
        raise SystemExit(1) from e


def serve():
    # Resolved before anything else: a node whose gateway port is unassigned or
    # unreachable cannot serve a single request, and every service it accepted
    # would be unable to call back into it.
    port = env_manager.get_gateway_port()
    _open_and_verify_gateway_port(port)

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
    server.wait_for_termination()
