import threading
from concurrent import futures

import grpc, json

from protos import celaut_pb2, celaut_pb2_grpc
from src.gateway.gateway import Gateway
from src.tunneling_system.tunnels import TunnelSystem
from src.manager.maintain import manager_thread
from src.utils.config import ConfigManager

env_manager = ConfigManager()
GATEWAY_PORT = env_manager.get_env("GATEWAY_PORT")

def serve():

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

    server.add_insecure_port('[::]:' + str(GATEWAY_PORT))

    server.start()
    server.wait_for_termination()
