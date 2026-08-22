import os
import sys
import threading
import time
from typing import Any, Generator
import contextlib
import io

import grpc

from bee_rpc.client import client_grpc
from protos import celaut_pb2, celaut_pb2_grpc, gateway_bee

from src.commands.inspect_service import inspect as inspect_service
from src.commands.__by_tag import get_id
from src.core_services.source_application import acquire_service
from src.manager.manager import get_execute_client
from src.utils.hashing import get_configured_hash_id
from src.utils.config import ConfigManager
from src.utils.instance_names import inject_instance_name

env_manager = ConfigManager()

METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")
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


# What the throwaway local dev client is funded with, in *our* MU.
#
# Not a price, and deliberately not an ERG figure: no real money moves for a dev
# client, so this only has to sit comfortably above whatever the node quotes
# (`build + default_initial_balance`), which the `pricing` config puts in the
# millions of MU per hour.
#
# It is emphatically *not* the instance's balance. The node derives that from the
# resources actually requested (`manager.default_initial_balance`) and, when the
# service is delegated, `configuration_for_peer` converts it to the executor's own
# MU scale. Passing one figure for both -- as this did -- made the quote come out
# above the very balance meant to pay it, and shipped a local MU figure to a peer
# that reads MU on a different scale.
DEV_CLIENT_FUNDING_MU = 10**12


def generator(
    _hash: str,
    client_funding_mu: int = DEV_CLIENT_FUNDING_MU,
    external: bool = False,
    envs: dict[str, str] | None = None,
    instance_name: str | None = None,
) -> Generator[Any, None, None]:
    try:
        client_id = get_execute_client(amount_mu=client_funding_mu, external=external)
    except Exception:
        raise RuntimeError("No execute client available.")

    try:
        yield celaut_pb2.Client(client_id=client_id)

        # No initial_mu: the node fills it from the requested resources, priced for
        # deposits.INITIAL_RUNTIME_HOURS, in its own MU.
        config = celaut_pb2.Configuration()
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


def launch_via_gateway(service: str, input_generator, success_message: str):
    """Run a `StartService` call against this node's own gateway daemon.

    Shared by `execute()` and `force_execution()`: same channel setup,
    launching animation, and friendly error mapping either way -- they only
    differ in how `input_generator` steers peer selection server-side.
    Returns the `ServiceInstance` response, or None (having already printed
    why) on any failure.
    """
    channel = None
    stop_event = threading.Event()
    animation_thread = threading.Thread(
        target=rocket_animation,
        args=(stop_event,),
        daemon=True,
    )
    try:
        channel = grpc.insecure_channel(f"localhost:{env_manager.get_gateway_port()}")
        g_stub = celaut_pb2_grpc.GatewayStub(channel)

        inspect_service(service)
        animation_thread.start()

        response = next(client_grpc(
            method=g_stub.StartService,
            input=input_generator,
            indices_parser=celaut_pb2.ServiceInstance,
            partitions_message_mode_parser=True,
            indices_serializer=gateway_bee.StartService_input_indices
        ))
        stop_event.set()
        animation_thread.join()
        print(success_message)
        return response

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

        return None

    except Exception as e:
        stop_event.set()
        if animation_thread.is_alive():
            animation_thread.join()
        print("❌ Unexpected error while launching service.")
        print(f"Details: {str(e)}")
        return None
    finally:
        if channel is not None:
            channel.close()


def print_endpoints(response) -> None:
    """Print the HTTP endpoints (if any) a `ServiceInstance` response exposes."""
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


def execute(
    service: str,
    external: bool = False,
    envs: dict[str, str] | None = None,
    instance_name: str | None = None,
    silent: bool = False
):
    sink = open(os.devnull, "w") if silent else None

    try:
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

        response = launch_via_gateway(
            service=service,
            input_generator=generator(
                _hash=service,
                external=external,
                envs=envs,
                instance_name=instance_name,
            ),
            success_message="🚀 Service launched successfully!\n",
        )
        if response is None:
            return

        print_endpoints(response)

    finally:
        if sink:
            sink.close()