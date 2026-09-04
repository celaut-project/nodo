import threading

from protos import celaut_pb2, celaut_pb2, celaut_pb2_grpc, gateway_bee
from bee_rpc.client import Dir, client_grpc

from src.identity.grpc_transport import verified_channel

from tests.main import SORTER, FRONTIER, WALL, WALK, REGRESION, RANDOM, GATEWAY, generator


def test_build():
    # Get solver cnf
    def build_method(hash: str):
        service = next(client_grpc(
            method=celaut_pb2_grpc.GatewayStub(
                verified_channel(GATEWAY),
            ).StartService,
            input=generator(_hash=hash),
            indices_parser=celaut_pb2.Instance,
            partitions_message_mode_parser=True,
            indices_serializer=gateway_bee.StartService_input_indices
        ))
        print('service ', hash, ' -> ', service)


    for s in [RANDOM, REGRESION, WALL, WALK, FRONTIER, SORTER]:
        print('Go to build ', s)
        threading.Thread(
            target=build_method,
            args=(s,)
        ).start()
