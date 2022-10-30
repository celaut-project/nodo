import os
import os
import threading
from concurrent import futures

import grpc
import grpcbigbuffer as grpcbf
import netifaces as ni
from psutil import virtual_memory

import src.manager.resources_manager as iobd
from protos import gateway_pb2, gateway_pb2_grpc
from src.gateway.server import Gateway
from src.manager.maintain_thread import manager_thread
from src.utils import logger as l
from src.utils.env import GATEWAY_PORT, MEMORY_LOGS, IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER, \
    SEND_ONLY_HASHES_ASKING_COST, DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH, REGISTRY, HYCACHE, LOCAL_NETWORK, \
    DOCKER_NETWORK, COMPUTE_POWER_RATE, COST_OF_BUILD, EXECUTION_BENEFIT, MANAGER_ITERATION_TIME, \
    COST_AVERAGE_VARIATION, GAS_COST_FACTOR, MODIFY_SERVICE_SYSTEM_RESOURCES_COST
from src.utils.zeroconf import Zeroconf

if __name__ == '__main__':
    # Create __hycache__ if it does not exists.
    try:
        os.system('mkdir ' + HYCACHE)
    except:
        pass

    # Create __registry__ if it does not exists.
    try:
        os.system('mkdir ' + REGISTRY)
    except:
        pass

    iobd.IOBigData(
        ram_pool_method=lambda: virtual_memory().total
    ).set_log(
        log=l.LOGGER if MEMORY_LOGS else lambda message: None
    )

    grpcbf.modify_env(
        cache_dir=HYCACHE,
        mem_manager=iobd.mem_manager
    )

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

    l.LOGGER('Starting gateway at port' + str(GATEWAY_PORT))

    server.start()
    server.wait_for_termination()
