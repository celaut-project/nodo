import os.path
from typing import Optional, Any, Generator
import grpc

from protos import celaut_pb2, gateway_pb2, gateway_pb2_grpc, gateway_pb2_grpcbf
from grpcbigbuffer.client import Dir, client_grpc

from src.database.access_functions.peers import get_peer_ids, get_peer_directions
from src.utils.env import SHA3_256_ID, EnvManager
from src.utils.utils import to_gas_amount
from src.manager.manager import get_dev_clients

env_manager = EnvManager()

GATEWAY_PORT = env_manager.get_env("GATEWAY_PORT")
METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")
REGISTRY = env_manager.get_env("REGISTRY")
DEFAULT_INTIAL_GAS_AMOUNT = env_manager.get_env("DEFAULT_INTIAL_GAS_AMOUNT")

SHA3_256 = SHA3_256_ID.hex()

def generator(_hash: str, mem_limit: int = 50 * pow(10, 6), initial_gas_amount: int = DEFAULT_INTIAL_GAS_AMOUNT) -> Generator[Any, None, None]:
    clients = get_dev_clients(gas_amount=initial_gas_amount)
    try:
        yield gateway_pb2.Client(client_id=next(clients))

        yield gateway_pb2.Configuration(
            config=celaut_pb2.Configuration(),
            resources=gateway_pb2.CombinationResources(
                clause={
                    1: gateway_pb2.CombinationResources.Clause(
                        cost_weight=1,
                        min_sysreq=celaut_pb2.Sysresources(
                                mem_limit=mem_limit
                            )
                    )
                }
            ),
            initial_gas_amount=to_gas_amount(initial_gas_amount)
        )

        yield celaut_pb2.Any.Metadata.HashTag.Hash(
                type=bytes.fromhex(SHA3_256),
                value=bytes.fromhex(_hash)
            )

        # Don't need to send metadata or service because it's on local.

    except Exception as e:
        print(f"Exception on executing {_hash[:6]}: {e}")


def execute(service: str):

    g_stub = gateway_pb2_grpc.GatewayStub(
        grpc.insecure_channel(f"localhost:{GATEWAY_PORT}"),
    )

    service = next(client_grpc(
        method=g_stub.StartService,
        input=generator(_hash=service),
        indices_parser=gateway_pb2.Instance,
        partitions_message_mode_parser=True,
        indices_serializer=gateway_pb2_grpcbf.StartService_input_indices
    ))
    print(f'service partition -> {service}')
