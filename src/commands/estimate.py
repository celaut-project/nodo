from protos import celaut_pb2

from src.commands.execute import resolve_service_hash
from src.manager.manager import get_dev_clients
from src.utils.bee_client import BeeClient
from src.utils.config import ConfigManager
from src.identity.grpc_transport import local_channel
from src.utils.monetary import format_mu
from src.utils.utils import (
    from_amount,
    read_metadata_from_disk,
    service_extended,
)

env_manager = ConfigManager()

# Balance the throwaway estimation client is given, only to authenticate the request;
# it is never spent, since estimating launches nothing, and never quoted.
ESTIMATION_BALANCE_MU = 10 ** 16


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
    clients = get_dev_clients(amount_mu=ESTIMATION_BALANCE_MU)
    try:
        client_id = next(clients)
    except StopIteration:
        print("There is no dev client available with enough balance.")
        return

    # No `initial_mu`: the quote's "to start" is the build plus the balance the instance
    # starts with, so asking with the throwaway balance would report that balance as the
    # cost. Left unset, the node funds the quote with its own default (the requested
    # resources for `deposits.INITIAL_RUNTIME_HOURS`), which is what a real launch pays.
    configuration = celaut_pb2.Configuration()

    channel = local_channel()

    print(f"Estimate {service}")
    print("Querying gateway for estimated cost (uses real-time locked RAM)...")

    try:
        estimated_cost = BeeClient.get_service_estimated_cost(
            channel,
            service_extended(
                metadata=metadata,
                config=configuration,
                send_only_hashes=True,   # service is local, only hash needed
                client_id=client_id,
            ),
        )
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
    loop_seconds = estimated_cost.maintenance_seconds_loop or 1
    per_hour = lambda amount: format_mu(int(from_amount(amount) * 3600 / loop_seconds))

    print("Estimated costs:")
    print(f"- To start:                 {format_mu(from_amount(estimated_cost.cost))}")
    print(f"- Maintenance, as started:  {per_hour(estimated_cost.init_maintenance_cost)} per hour")
    print(f"- Maintenance, at its most: {per_hour(estimated_cost.max_maintenance_cost)} per hour")
    print(f"- Charged every:            {estimated_cost.maintenance_seconds_loop} seconds")
