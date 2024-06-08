# Test combine method.
from typing import Final

import grpc, sys
from grpcbigbuffer.client import Dir, client_grpc
from src.utils.env import METADATA_REGISTRY, REGISTRY

from src.utils.logger import LOGGER
from tests.main import *
from protos import gateway_pb2, celaut_pb2, gateway_pb2_grpc, gateway_pb2_grpcbf


def test_start_service():

    try:
        SERVICE: Final[str] = eval(sys.argv[3])
    except IndexError:
        LOGGER('Provide the name of a service (from .services) as the third parameter.')
    except SyntaxError:
        LOGGER('The third parameter must be one of the services on tests/.services')

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
        yield Dir(dir=METADATA_REGISTRY + SERVICE, _type=celaut_pb2.Any.Metadata)
        yield Dir(dir=REGISTRY + SERVICE, _type=celaut_pb2.Service)


    g_stub = gateway_pb2_grpc.GatewayStub(
        grpc.insecure_channel(GATEWAY),
    )

    service = next(client_grpc(
        method=g_stub.StartService,
        input=service_extended(),
        indices_parser=gateway_pb2.Instance,
        partitions_message_mode_parser=True,
        indices_serializer=gateway_pb2_grpcbf.StartService_input_indices
    ))
    print(f'service partition -> {service}')
