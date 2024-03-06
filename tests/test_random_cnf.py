import sys
from time import sleep

import grpc
from grpcbigbuffer.client import client_grpc

from tests.protos import api_pb2, api_pb2_grpc
from protos import gateway_pb2, gateway_pb2_grpc
from protos.gateway_pb2_grpcbf import StartService_input_indices
from tests.main import RANDOM, SORTER, FRONTIER, GATEWAY, generator as gen


def generator(_hash: str, mem_limit: int = 50 * pow(10, 6), initial_gas_amount: int = pow(10, 16)):
    yield from gen(_hash=_hash, mem_limit=mem_limit, initial_gas_amount=initial_gas_amount)

def test_random_cnf():
    g_stub = gateway_pb2_grpc.GatewayStub(
            grpc.insecure_channel(GATEWAY)
        )

    # Get random cnf

    random_cnf_service = next(client_grpc(
        method=g_stub.StartService,
        input=generator(_hash=RANDOM),
        indices_parser=gateway_pb2.Instance,
        partitions_message_mode_parser=True,
        indices_serializer=StartService_input_indices
    ))

    print(f"Random cnf service {random_cnf_service}")
    uri = random_cnf_service.instance.uri_slot[0].uri[0]
    r_uri = uri.ip + ':' + str(uri.port)

    r_stub = api_pb2_grpc.RandomStub(
        grpc.insecure_channel(r_uri)
    )
    print('Received random. ', r_stub)
    
    while True:
        sleep(5)
        try:
            cnf = next(client_grpc(
                    method=r_stub.RandomCnf,
                    partitions_message_mode_parser=True,
                    indices_parser=api_pb2.Cnf
                ))
            break
        except: pass

    print(f"cnf {cnf}")
