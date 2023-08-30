# Test combine method.
import grpc
from grpcbigbuffer.client import Dir, client_grpc

from main import GATEWAY, SHA3_256, SORTER, FRONTIER
from protos import gateway_pb2, celaut_pb2, gateway_pb2_grpc, gateway_pb2_grpcbf

SERVICE = SORTER


def service_extended():
    # Send partition model.
    yield gateway_pb2.Client(client_id='dev')
    config = gateway_pb2.Configuration(
        config=celaut_pb2.Configuration(),
        resources=gateway_pb2.CombinationResources(
            clause={
                1: gateway_pb2.CombinationResources.Clause(
                    cost_weight=1,
                    min_sysreq=celaut_pb2.Sysresources(
                        mem_limit=50 * pow(10, 6)
                    )
                )
            }
        )
    )
    yield config
    yield celaut_pb2.Any.Metadata.HashTag.Hash(
            type=bytes.fromhex(SHA3_256),
            value=bytes.fromhex(SERVICE)
        )
    yield Dir(dir='__metadata__/' + SERVICE, _type=celaut_pb2.Any.Metadata)
    yield Dir(dir='__registry__/' + SERVICE, _type=celaut_pb2.Service)


g_stub = gateway_pb2_grpc.GatewayStub(
    grpc.insecure_channel(GATEWAY + ':8090'),
)

service = next(client_grpc(
    method=g_stub.StartService,
    input=service_extended(),
    indices_parser=gateway_pb2.Instance,
    partitions_message_mode_parser=True,
    indices_serializer=gateway_pb2_grpcbf.StartService_input_indices
))
print(f'service partition -> {service}')
