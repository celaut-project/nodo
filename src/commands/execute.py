import os
from typing import Any, Generator

import grpc

from bee_rpc.client import client_grpc
from protos import celaut_pb2, celaut_pb2_grpc, gateway_bee

from src.commands.__by_tag import get_id
from src.manager.manager import get_dev_clients
from src.utils.hashing import get_configured_hash_id
from src.utils.config import ConfigManager
from src.utils.utils import to_gas_amount

env_manager = ConfigManager()

GATEWAY_PORT = env_manager.get("GATEWAY_PORT")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")
DEFAULT_INITIAL_GAS_AMOUNT = env_manager.get("DEFAULT_INITIAL_GAS_AMOUNT")
CONFIGURED_HASH_ID = get_configured_hash_id(env_manager)


def resolve_service_hash(service: str) -> str:
    resolved_service = get_id(service)
    service = resolved_service if resolved_service else service

    if os.path.exists(os.path.join(REGISTRY, service)):
        return service

    try:
        for selected in os.listdir(METADATA_REGISTRY):
            with open(os.path.join(METADATA_REGISTRY, selected), "rb") as f:
                metadata = celaut_pb2.Metadata()
                metadata.ParseFromString(f.read())
                first_tag = metadata.hashtag.tag[0] if len(metadata.hashtag.tag) > 0 else ""
                if str(first_tag) == str(service):
                    return selected
    except Exception:
        return ""

    return ""


def generator(_hash: str, mem_limit: int = 50 * pow(10, 4), initial_gas_amount: int = DEFAULT_INITIAL_GAS_AMOUNT) -> Generator[Any, None, None]:
    print("Get clients")
    clients = get_dev_clients(gas_amount=initial_gas_amount)
    try:
        client_id = next(clients)
    except Exception:
        print("There is no dev client available.")
        raise RuntimeError("No dev client available.")
    print(f"Client obtained {str(client_id)}")
    try:
        
        print("Send client")
        yield celaut_pb2.Client(client_id=client_id)
        print("Client sent")

        print("Send configuration")
        yield celaut_pb2.Configuration(
            initial_gas_amount=to_gas_amount(initial_gas_amount)
        )
        print("Configuration sent")

        print(f"Send hash {_hash}")
        yield celaut_pb2.Metadata.HashTag.Hash(
                type=CONFIGURED_HASH_ID,
                value=bytes.fromhex(_hash)
            )
        print(f"Hash {_hash} sent.")

        # Don't need to send metadata or service because it's on local.

    except Exception as e:
        print(f"Exception on executing {_hash[:6]}: {e}")


def execute(service: str):
    service = resolve_service_hash(service)
    if not service:
        print("No service allowed.")
        return

    channel = None
    try:
        channel = grpc.insecure_channel(f"localhost:{GATEWAY_PORT}")
        g_stub = celaut_pb2_grpc.GatewayStub(channel)

        print(f"Execute {service}")

        response = next(client_grpc(
            method=g_stub.StartService,
            input=generator(
                _hash=service,
                initial_gas_amount=10**16,
                mem_limit=10**9
            ),
            indices_parser=celaut_pb2.ServiceInstance,
            partitions_message_mode_parser=True,
            indices_serializer=gateway_bee.StartService_input_indices
        ))

        print(f"service partition -> {response}")

    except grpc.RpcError as e:
        # Handle gRPC-specific errors cleanly
        status_code = e.code()
        details = e.details()

        FRIENDLY_ERRORS = {
            grpc.StatusCode.NOT_FOUND: "Service not found.",
            grpc.StatusCode.UNAVAILABLE: "Gateway is unavailable.",
            grpc.StatusCode.PERMISSION_DENIED: "Permission denied.",
            grpc.StatusCode.DEADLINE_EXCEEDED: "Request timed out."
        }

        print("\n[ERROR] Failed to execute service.")
        message = FRIENDLY_ERRORS.get(status_code, "Unknown error occurred.")
        print(f"Reason: {message}")

        if details:
            print(f"Details: {details}")

        return

    except Exception as e:
        # Catch any unexpected errors
        print(f"\n[ERROR] Service could not be executed.")
        print(f"Details: {str(e)}")
        return
    finally:
        if channel is not None:
            channel.close()

    # Process HTTP endpoints only if execution succeeded
    for slot in response.instance.api.slot:
        if "http" in slot.protocol_stack[0].tags:
            for _exp in response.instance.uri_slot:
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
