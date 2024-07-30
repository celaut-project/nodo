import threading
from concurrent import futures

import grpc
import netifaces as ni

from protos import gateway_pb2, gateway_pb2_grpc
from src.gateway.gateway import Gateway
from src.tunneling_system.tunnels import TunnelSystem
from src.manager.maintain_thread import manager_thread
from src.utils import logger as l
from src.utils.zeroconf import Zeroconf
from src.utils.env import GATEWAY_PORT, IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER, \
    SEND_ONLY_HASHES_ASKING_COST, DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH, LOCAL_NETWORK, \
    DOCKER_NETWORK, COMPUTE_POWER_RATE, COST_OF_BUILD, EXECUTION_BENEFIT, MANAGER_ITERATION_TIME, \
    COST_AVERAGE_VARIATION, GAS_COST_FACTOR, MODIFY_SERVICE_SYSTEM_RESOURCES_COST, EXTERNAL_COST_TIMEOUT


def serve():
    # Zeroconf for connect to the network (one per network).
    for network in ni.interfaces():
        if network != DOCKER_NETWORK and network != LOCAL_NETWORK:
            Zeroconf(network=network)

    # Run manager.
    threading.Thread(
        target=manager_thread,
        daemon=True
    ).start()

    # create a gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=30))
    gateway_pb2_grpc.add_GatewayServicer_to_server(
        Gateway(), server=server
    )

    SERVICE_NAMES = (
        gateway_pb2.DESCRIPTOR.services_by_name['Gateway'].full_name,
    )

    server.add_insecure_port('[::]:' + str(GATEWAY_PORT))

    l.LOGGER('COMPUTE POWER RATE -> ' + str(COMPUTE_POWER_RATE))
    l.LOGGER('COST OF BUILD -> ' + str(COST_OF_BUILD))
    l.LOGGER('EXECUTION BENEFIT -> ' + str(EXECUTION_BENEFIT))
    l.LOGGER('IGNORE FATHER NETWORK ON SERVICE BALANCER -> ' + str(IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER))
    l.LOGGER('SEND ONLY HASHES ASKING COST -> ' + str(SEND_ONLY_HASHES_ASKING_COST))
    l.LOGGER('DENEGATE COST REQUEST IF DONT VE THE HASH -> ' + str(DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH))
    l.LOGGER('MANAGER ITERATION TIME-> ' + str(MANAGER_ITERATION_TIME))
    l.LOGGER('AVG COST MAX PROXIMITY FACTOR-> ' + str(COST_AVERAGE_VARIATION))
    l.LOGGER('GAS_COST_FACTOR-> ' + str(GAS_COST_FACTOR))
    l.LOGGER('MODIFY_SERVICE_SYSTEM_RESOURCES_COST_FACTOR-> ' + str(MODIFY_SERVICE_SYSTEM_RESOURCES_COST))
    l.LOGGER('EXTERNAL_COST_TIMEOUT -> ' + str(EXTERNAL_COST_TIMEOUT))

    l.LOGGER('Starting gateway at port' + str(GATEWAY_PORT))
    l.LOGGER(f'Tunnel available at {TunnelSystem().get_url()}')

    server.start()
    server.wait_for_termination()
