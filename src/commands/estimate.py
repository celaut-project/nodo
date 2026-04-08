import grpc
from bee_rpc import client as bee

from protos import celaut_pb2, celaut_pb2_grpc
from protos.gateway_bee import StartService_input_indices

from src.commands.execute import resolve_service_hash
from src.manager.manager import get_dev_clients
from src.utils.config import ConfigManager
from src.utils.utils import (
    from_gas_amount,
    read_metadata_from_disk,
    to_gas_amount,
    service_extended,
)

env_manager = ConfigManager()

GATEWAY_PORT = env_manager.get("GATEWAY_PORT")
ESTIMATION_INITIAL_GAS_AMOUNT = 10 ** 16


def estimate(service: str) -> None:
    service = resolve_service_hash(service)
    if not service:
        print("No service allowed.")
        return

    metadata = read_metadata_from_disk(service_hash=service)
    if not metadata:
        print(f"Cannot estimate cost. Metadata for service {service} not found.")
        return

    # Obtain a dev client to authenticate the request.
    clients = get_dev_clients(gas_amount=ESTIMATION_INITIAL_GAS_AMOUNT)
    try:
        client_id = next(clients)
    except StopIteration:
        print("There is no dev client available with enough gas.")
        return

    configuration = celaut_pb2.Configuration(
        initial_gas_amount=to_gas_amount(gas_amount=ESTIMATION_INITIAL_GAS_AMOUNT)
    )

    channel = grpc.insecure_channel(f"localhost:{GATEWAY_PORT}")
    g_stub = celaut_pb2_grpc.GatewayStub(channel)

    print(f"Estimate {service}")
    print("Querying gateway for estimated cost (uses real-time locked RAM)...")

    try:
        estimated_cost = next(bee.client_grpc(
            method=g_stub.GetServiceEstimatedCost,
            input=service_extended(
                metadata=metadata,
                config=configuration,
                send_only_hashes=True,   # service is local, only hash needed
                client_id=client_id,
            ),
            indices_parser=celaut_pb2.EstimatedCost,
            partitions_message_mode_parser=True,
            indices_serializer=StartService_input_indices,
        ), None)
    except Exception as e:
        print("Execution feasibility: NO")
        print(f"Reason: gateway error — {str(e)}")
        return
    finally:
        channel.close()

    if not estimated_cost:
        print("Execution feasibility: NO")
        print("Reason: gateway could not generate a valid estimated cost (insufficient resources or unsupported architecture).")
        return

    print("Execution feasibility: YES")
    print("Estimated costs (gas units):")
    print(f"- Initial cost:             {from_gas_amount(estimated_cost.cost)}")
    print(f"- Initial maintenance:      {from_gas_amount(estimated_cost.init_maintenance_cost)}")
    print(f"- Max maintenance:          {from_gas_amount(estimated_cost.max_maintenance_cost)}")
    print(f"- Maintenance loop (secs):  {estimated_cost.maintenance_seconds_loop}")
