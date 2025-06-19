from typing import Any, Generator
import grpc
import os

from protos import celaut_pb2, celaut_pb2, celaut_pb2_grpc, gateway_bee
from bee_rpc.client import client_grpc

from src.utils.env import SHA3_256_ID, EnvManager
from src.utils.utils import to_gas_amount
from src.manager.manager import get_dev_clients
from src.commands.__by_tag import get_id

env_manager = EnvManager()

GATEWAY_PORT = env_manager.get_env("GATEWAY_PORT")
METADATA_REGISTRY = env_manager.get_env("METADATA_REGISTRY")
REGISTRY = env_manager.get_env("REGISTRY")
DEFAULT_INTIAL_GAS_AMOUNT = env_manager.get_env("DEFAULT_INTIAL_GAS_AMOUNT")

SHA3_256 = SHA3_256_ID.hex()

def generator(_hash: str, mem_limit: int = 50 * pow(10, 4), initial_gas_amount: int = DEFAULT_INTIAL_GAS_AMOUNT) -> Generator[Any, None, None]:
    print("Get clients")
    clients = get_dev_clients(gas_amount=initial_gas_amount)
    try:
        client_id = next(clients)
    except Exception:
        print("There is no dev client available.")
        exit()
    print(f"Client obtained {str(client_id)}")
    try:
        
        print("Send client")
        yield celaut_pb2.Client(client_id=client_id)
        print("Client sent")

        print("Send configuration")
        yield celaut_pb2.Configuration(
            resources=celaut_pb2.Service.Container.CombinationResources(
                clause={
                    1: celaut_pb2.Service.Container.CombinationResources.Clause(
                        cost_weight=1,
                        min_sysreq=celaut_pb2.Sysresources(
                            mem_limit=mem_limit
                        )
                    )
                }
            ),
            initial_gas_amount=to_gas_amount(initial_gas_amount)
        )
        print("Configuration sent")

        print(f"Send hash {_hash}")
        yield celaut_pb2.Metadata.HashTag.Hash(
                type=bytes.fromhex(SHA3_256),
                value=bytes.fromhex(_hash)
            )
        print(f"Hash {_hash} sent.")

        # Don't need to send metadata or service because it's on local.

    except Exception as e:
        print(f"Exception on executing {_hash[:6]}: {e}")


def execute(service: str):
    service = get_id(service)

    g_stub = celaut_pb2_grpc.GatewayStub(
        grpc.insecure_channel(f"localhost:{GATEWAY_PORT}"),
    )
    
    if not os.path.exists(os.path.join(REGISTRY, service)):
        found = False
        try:
            for selected in os.listdir(os.path.join(METADATA_REGISTRY)):
                with open(os.path.join(METADATA_REGISTRY, selected), "rb") as f:
                    metadata = celaut_pb2.Metadata()
                    metadata.ParseFromString(f.read())
                    first_tag = metadata.hashtag.tag[0] if len(metadata.hashtag.tag) > 0 else ""
                    if str(first_tag) == str(service):
                        service = selected
                        found = True
                        break
            if not found: raise Exception
        except Exception as e:
            print(f"Error: {str(e)}")
            print("No service allowed.")
            return

    print(f"Execute {service}")
    service = next(client_grpc(
        method=g_stub.StartService,
        input=generator(
            _hash=service,
            initial_gas_amount=10**16,
            mem_limit=10**9
        ),
        indices_parser=celaut_pb2.GatewayInstance,
        partitions_message_mode_parser=True,
        indices_serializer=gateway_bee.StartService_input_indices
    ))
    print(f'service partition -> {service}')
    
    # Indicate http endpoints to allow more friendly usage.
    for slot in service.instance.api.slot:
        if "http" in slot.protocol_stack[0].tags: 
            for _exp in service.instance.uri_slot:
                if _exp.internal_port == slot.port:
                    print("\n" + "="*50)
                    print("="*50 + "\n")
                    print(f"  🔍 HTTP Service (Port: {slot.port})")
                    print("="*50)
                    print("  🌐 Available Endpoints:")
                    print("-"*50)
                    for _uri in _exp.uri:
                        print(f"  • http://{_uri.ip}:{_uri.port}")
                    print("="*50 + "\n")
                    break