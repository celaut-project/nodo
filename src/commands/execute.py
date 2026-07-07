import os
import sys
import threading
import time
from typing import Any, Generator

import grpc

from bee_rpc.client import client_grpc
from protos import celaut_pb2, celaut_pb2_grpc, gateway_bee

from src.commands.inspect import inspect as inspect_service
from src.commands.__by_tag import get_id
from src.core_services.source_application import acquire_service
from src.manager.manager import get_execute_client
from src.utils.hashing import get_configured_hash_id
from src.utils.config import ConfigManager
from src.utils.instance_names import inject_instance_name
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


def generator(
    _hash: str,
    mem_limit: int = 50 * pow(10, 4),
    initial_gas_amount: int = DEFAULT_INITIAL_GAS_AMOUNT,
    external: bool = False,
    envs: dict[str, str] | None = None,
    instance_name: str | None = None,
) -> Generator[Any, None, None]:
    try:
        client_id = get_execute_client(gas_amount=initial_gas_amount, external=external)
    except Exception:
        raise RuntimeError("No execute client available.")

    try:
        yield celaut_pb2.Client(client_id=client_id)

        config = celaut_pb2.Configuration(
            initial_gas_amount=to_gas_amount(initial_gas_amount)
        )
        if envs:
            config.environment_variables.update({
                k: v.encode() for k, v in envs.items()
            })
        inject_instance_name(config=config, instance_name=instance_name)
        yield config

        yield celaut_pb2.Metadata.HashTag.Hash(
                type=CONFIGURED_HASH_ID,
                value=bytes.fromhex(_hash)
            )

        # Don't need to send metadata or service because it's on local.

    except Exception as e:
        raise RuntimeError(f"Exception on executing {_hash[:6]}: {e}") from e


def rocket_animation(stop_event: threading.Event):
    frames = [
        "🚀      ",
        " 🚀     ",
        "  🚀    ",
        "   🚀   ",
        "    🚀  ",
        "     🚀 ",
        "      🚀",
        "     🚀 ",
        "    🚀  ",
        "   🚀   ",
        "  🚀    ",
        " 🚀     ",
    ]

    index = 0
    while not stop_event.is_set():
        frame = frames[index % len(frames)]
        sys.stdout.write(f"\rLaunching service... {frame}")
        sys.stdout.flush()
        time.sleep(0.1)
        index += 1

    sys.stdout.write("\r" + " " * 50 + "\r")
    sys.stdout.flush()


def execute(
    service: str,
    external: bool = False,
    envs: dict[str, str] | None = None,
    instance_name: str | None = None,
):
    resolved = resolve_service_hash(service)
    if not resolved:
        # The service isn't in the local registry. Before refusing, try to acquire it
        # through the 'source-application' core service: it maps the requested service id
        # to its published sources and downloads it via the existing download/import path.
        # This only succeeds when a trusted source-application is configured in
        # 'core_services'; otherwise it's a no-op and we fall through to the error below.
        if acquire_service(service):
            resolved = resolve_service_hash(service)

    if not resolved:
        print("❌ Service not allowed.")
        return

    service = resolved

    channel = None
    stop_event = threading.Event()
    animation_thread = threading.Thread(
        target=rocket_animation,
        args=(stop_event,),
        daemon=True,
    )
    try:
        channel = grpc.insecure_channel(f"localhost:{GATEWAY_PORT}")
        g_stub = celaut_pb2_grpc.GatewayStub(channel)

        inspect_service(service)
        animation_thread.start()

        response = next(client_grpc(
            method=g_stub.StartService,
            input=generator(
                _hash=service,
                initial_gas_amount=10**16,
                mem_limit=10**9,
                external=external,
                envs=envs,
                instance_name=instance_name,
            ),
            indices_parser=celaut_pb2.ServiceInstance,
            partitions_message_mode_parser=True,
            indices_serializer=gateway_bee.StartService_input_indices
        ))
        stop_event.set()
        animation_thread.join()
        print("🚀 Service launched successfully!\n")

    except grpc.RpcError as e:
        stop_event.set()
        if animation_thread.is_alive():
            animation_thread.join()

        status_code = e.code()
        details = e.details()

        FRIENDLY_ERRORS = {
            grpc.StatusCode.NOT_FOUND: "Service not found.",
            grpc.StatusCode.UNAVAILABLE: "Gateway is unavailable.",
            grpc.StatusCode.PERMISSION_DENIED: "Permission denied.",
            grpc.StatusCode.DEADLINE_EXCEEDED: "Request timed out."
        }

        print("❌ Failed to launch service.")
        message = FRIENDLY_ERRORS.get(status_code, "Unknown error occurred.")
        print(f"Reason: {message}")

        if details:
            print(f"Details: {details}")

        return

    except Exception as e:
        stop_event.set()
        if animation_thread.is_alive():
            animation_thread.join()
        print("❌ Unexpected error while launching service.")
        print(f"Details: {str(e)}")
        return
    finally:
        if channel is not None:
            channel.close()

    endpoints: list[str] = []
    for slot in response.instance.api.slot:
        protocol_tags = {
            tag.lower()
            for protocol in slot.protocol_stack
            for tag in protocol.tags
        }
        transport_tags = {tag.lower() for tag in slot.transport.tags}
        if "http" in protocol_tags or "http" in transport_tags:
            for _exp in response.instance.uri_slot:
                if _exp.internal_port == slot.port:
                    for _uri in _exp.uri:
                        endpoints.append(f"http://{_uri.ip}:{_uri.port}")
                    break

    if endpoints:
        print("🌐 Endpoints available:\n")
        for endpoint in endpoints:
            print(f"  • {endpoint}")
    else:
        print("No endpoints available")
