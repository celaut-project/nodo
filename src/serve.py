import threading
from concurrent import futures

import grpc

from protos import celaut_pb2, celaut_pb2_grpc
from src.gateway.gateway import Gateway
from src.manager.maintain import manager_thread
from src.tunneling import delegated_endpoints
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.grpc_transport import server_credentials

env_manager = ConfigManager()
GATEWAY_PORT = env_manager.get("GATEWAY_PORT")
GATEWAY_PLAINTEXT_PORT = env_manager.get("network.GATEWAY_PLAINTEXT_PORT", 0)

def serve():

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
    server.add_secure_port('[::]:' + str(GATEWAY_PORT), server_credentials())

    # ...and, optionally, the same servicer in plain gRPC on a second port, for the
    # services this node runs -- they speak plain gRPC and are handed this port in
    # their `__config__.gateway` -- and for external callers that do not want TLS.
    # One `Gateway()` on two ports rather than a second server: same handlers, same
    # threads, no duplicated path to keep in step. TLS is what we *offer*; requiring
    # it of a service we execute would mean shipping certificate pinning into every
    # service SDK for a hop that never leaves the host.
    if GATEWAY_PLAINTEXT_PORT:
        bound = server.add_insecure_port('[::]:' + str(GATEWAY_PLAINTEXT_PORT))
        if bound:
            log.LOGGER(f'Serving plain gRPC on port {bound} (TLS on {GATEWAY_PORT}).')
        else:
            # Not fatal for peers, but every service launched from now on is handed this
            # port in its __config__ and will find nothing listening, so it must not be
            # silent.
            log.LOGGER(
                f'Could not bind the plaintext gateway port {GATEWAY_PLAINTEXT_PORT} '
                '-- services will be handed an address that answers nothing. Free that '
                'port, or point network.GATEWAY_PLAINTEXT_PORT at another one (0 makes '
                'services use the TLS port).'
            )

    server.start()
    server.wait_for_termination()
