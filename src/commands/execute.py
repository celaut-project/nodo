import os
import sys
import threading
import time
from typing import Any, Generator
import contextlib
import io

import grpc

from protos import celaut_pb2

from src.commands.inspect_service import inspect as inspect_service
from src.utils.bee_client import BeeClient
from src.commands.__by_tag import get_id
from src.core_services.source_application import acquire_service
from src.manager.manager import get_execute_client
from src.identity.grpc_transport import local_channel
from src.utils.hashing import get_configured_hash_id
from src.utils.config import ConfigManager
from src.utils import keyvalue
from src.utils.host_interface import HOST_EXPOSURE_KEY, HostInterfaceUnresolved, resolve_from_config
from src.utils.instance_names import inject_instance_name
from src.utils.registry_errors import ServiceRegistryError

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
    envs: dict[str, str] | None = None,
    instance_name: str | None = None,
) -> Generator[Any, None, None]:
    try:
        client_id = get_execute_client(amount_mu=client_funding_mu)
    except Exception:
        raise RuntimeError("No execute client available.")

    try:
        yield celaut_pb2.Client(client_id=client_id)

        # No initial_mu: the node fills it from the requested resources, priced for
        # deposits.INITIAL_RUNTIME_HOURS, in its own MU.
        config = celaut_pb2.Configuration()
        if envs:
            keyvalue.update(config.environment_variables, {
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
        channel = local_channel()

        try:
            inspect_service(service)
        except ServiceRegistryError as e:
            stop_event.set()
            print("❌ Failed to read service data during launch service.")
            print(f"Reason: {e}")
            return None
    
        animation_thread.start()

        response = BeeClient.start_service(channel, input_generator)
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


#: `execute --remote` is gone (#437): the address `execute` hands back is only
#: meaningful on the machine that ran it, and `nodo tunnel` is the one way to reach
#: an instance from anywhere else. Refused loudly rather than ignored, so a script
#: still passing it learns why instead of getting a loopback address that looks right.
REMOVED_REMOTE_FLAG_ERROR = (
    "Error: `nodo execute --remote` was removed. The address `execute` prints is only "
    "reachable from this host; to reach the instance from elsewhere use "
    "`nodo tunnel <instance> <slot> --peer <node address>:<gateway port>` "
    "(see docs/TUNNELING.md). To have `execute` also publish on this host's own "
    f"interface, set {HOST_EXPOSURE_KEY}."
)


def reject_removed_remote_flag(args: list[str]) -> None:
    """Exit with ``REMOVED_REMOTE_FLAG_ERROR`` if ``args`` still carries `--remote`."""
    if "--remote" in args:
        print(REMOVED_REMOTE_FLAG_ERROR, flush=True)
        sys.exit(1)


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


def print_host_exposure_note(response) -> None:
    """With ``HOST_EXPOSURE_KEY`` on, say whether the instance made it onto the host interface.

    The gateway resolves the address from the same config this process reads, so the
    CLI re-resolves it to tell a published instance (its URIs carry that address)
    from one that stayed internal -- and says why, instead of letting an internal
    address pass for a host one.
    """
    if not env_manager.get(HOST_EXPOSURE_KEY, False) or not response.instance.uri_slot:
        return
    advertised = {uri.ip for uri_slot in response.instance.uri_slot for uri in uri_slot.uri}
    try:
        host_ip = resolve_from_config(env_manager.get)
    except HostInterfaceUnresolved as e:
        reason = f"no host interface address resolves ({e}); set network.EXTERNAL_INTERFACE or network.PUBLIC_IP"
    else:
        if host_ip in advertised:
            print(f"\n  Published on this host's interface ({host_ip}) by {HOST_EXPOSURE_KEY}.")
            return
        reason = "the gateway kept it internal (see the gateway log)"

    slot = next((uri_slot.internal_port for uri_slot in response.instance.uri_slot), "<slot>")
    token = response.token or "<instance>"
    print(
        f"\n  Warning: {HOST_EXPOSURE_KEY} is on, but the instance was not published on "
        f"this host's interface: {reason}.\n"
        "  The address above is internal to the node's host. To reach the instance from "
        "elsewhere:\n"
        f"      nodo tunnel {token} {slot} --peer <node address>:<gateway port>"
    )


def execute(
    service: str,
    envs: dict[str, str] | None = None,
    instance_name: str | None = None,
    silent: bool = False
):
    sink = open(os.devnull, "w") if silent else None

    try:
        resolved = resolve_service_hash(service)
        if not resolved:
            # The service isn't in the local registry. Before refusing, try to acquire it:
            # first from the known peers, then through the 'source-application' core
            # service, which maps the requested service id to its published sources and
            # downloads it via the existing download/import path. If neither provides it,
            # we fall through to the error below.
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
                envs=envs,
                instance_name=instance_name,
            ),
            success_message="🚀 Service launched successfully!\n",
        )
        if response is None:
            return

        print_endpoints(response)
        print_host_exposure_note(response)

    finally:
        if sink:
            sink.close()