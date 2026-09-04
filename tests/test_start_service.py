# Test combine method.
from typing import Final

import sys
from bee_rpc.client import Dir, client_grpc

from src.identity.grpc_transport import verified_channel
from src.utils.logger import LOGGER

from tests.main import *
from protos import celaut_pb2, celaut_pb2, celaut_pb2_grpc, gateway_bee
from src.utils.config import ConfigManager

env_manager = ConfigManager()

METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")

def test_start_service():

    try:
        SERVICE: Final[str] = eval(sys.argv[3])
    except IndexError:
        LOGGER('Provide the name of a service (from .services) as the third parameter.')
    except SyntaxError:
        LOGGER('The third parameter must be one of the services on tests/.services')

    def service_extended():
        # Send partition model.
        yield celaut_pb2.Client(client_id='dev')
        yield celaut_pb2.Metadata.HashTag.Hash(
                type=bytes.fromhex(SHA3_256),
                value=bytes.fromhex(SERVICE)
            )
        yield Dir(dir=METADATA_REGISTRY + SERVICE, _type=celaut_pb2.Metadata)
        yield Dir(dir=REGISTRY + SERVICE, _type=celaut_pb2.Service)


    g_stub = celaut_pb2_grpc.GatewayStub(
        verified_channel(GATEWAY),
    )

    service = next(client_grpc(
        method=g_stub.StartService,
        input=service_extended(),
        indices_parser=celaut_pb2.Instance,
        partitions_message_mode_parser=True,
        indices_serializer=gateway_bee.StartService_input_indices
    ))
    print(f'service partition -> {service}')
