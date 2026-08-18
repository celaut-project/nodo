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

    # TLS on the single listener that serves peers and the local CLI alike (issue
    # #257): the certificate proves this node's identity key, so a caller can tell
    # it reached us and not whoever holds the address now. No plaintext port is
    # opened -- a node with no identity keypair cannot serve, by design.
    server.add_secure_port('[::]:' + str(GATEWAY_PORT), server_credentials())

    server.start()
    server.wait_for_termination()
