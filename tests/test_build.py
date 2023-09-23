import threading

from protos import celaut_pb2, gateway_pb2, gateway_pb2_grpc, gateway_pb2_grpcbf
import grpc
from grpcbigbuffer.client import Dir, client_grpc

from tests.main import SORTER, FRONTIER, WALL, WALK, REGRESION, RANDOM, GATEWAY, generator


def test_build():
    # Get solver cnf
    def build_method(hash: str):
        service = next(client_grpc(
            method=gateway_pb2_grpc.GatewayStub(
                grpc.insecure_channel(GATEWAY ),
            ).StartService,
            input=generator(_hash=hash),
            indices_parser=gateway_pb2.Instance,
            partitions_message_mode_parser=True,
            indices_serializer=gateway_pb2_grpcbf.StartService_input_indices
        ))
        print('service ', hash, ' -> ', service)


    for s in [RANDOM, REGRESION, WALL, WALK, FRONTIER, SORTER]:
        print('Go to build ', s)
        threading.Thread(
            target=build_method,
            args=(s,)
        ).start()
