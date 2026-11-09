import math

from protos import celaut_pb2

from src.commands.execute import resolve_service_hash
from src.manager.manager import get_dev_clients
from src.utils.bee_client import BeeClient
from src.utils.config import ConfigManager
from src.identity.grpc_transport import local_channel
from src.utils.cost_functions import price_bound
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
# The runs that `nodo estimate` shows the funding of: cost plus the maintenance sum.
FUNDING_HOURS = (1, 24)


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

    # No `initial_mu`: the quote's "to start" is the least `initial_mu` that starts the
    # service (#563). It is the build plus the balance that the instance has after it.
    # Asking with the throwaway balance would report that balance as the cost. Left
    # unset, the node funds the quote with its own default (the requested resources for
    # `deposits.INITIAL_RUNTIME_HOURS`). "To start" includes the starts of the initial
    # children of the service (#538): a real launch with no `initial_mu` pays it.
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
    print(f"- To start (initial_mu):    {format_mu(from_amount(estimated_cost.cost))}")
    print(f"- Maintenance, as started:  {per_hour(estimated_cost.init_maintenance_cost)} per hour")
    print(f"- Maintenance, at its most: {per_hour(estimated_cost.max_maintenance_cost)} per hour")
    print(f"- Charged every:            {estimated_cost.maintenance_seconds_loop} seconds")
    for line in price_bound_lines(estimated_cost):
        print(line)


def _period_text(seconds: int) -> str:
    if seconds <= 0:
        return "for the whole life of the instance"
    if seconds % 3600 == 0:
        return f"per {seconds // 3600} h"
    return f"per {seconds} s"


def price_bound_lines(estimated_cost, hours=FUNDING_HOURS):
    """The price bound of a quote and what a client sends to fund it (#636).

    The funding is the formula of ``EstimatedCost.cost`` in protos/celaut.proto:
    ``cost`` plus, for each maintenance period ``i``, ``init_maintenance_cost`` times
    ``(1 + price_volatility)^k(i)`` (``price_bound.funding_sum_mu``).
    """
    bound = price_bound.of_quote(estimated_cost)
    loop_seconds = int(estimated_cost.maintenance_seconds_loop or 0)
    lines = ["Price bound:"]
    if bound is None:
        lines.append("- Bounded: no. The node promises no limit: the price can rise with its load.")
    else:
        lines.append(
            f"- Bounded: yes. The price rises at most {bound.volatility * 100:g} % "
            f"{_period_text(bound.period_seconds)}, from the launch "
            f"(price_volatility {bound.volatility:g}, volatility_period_seconds {bound.period_seconds})."
        )
    if loop_seconds <= 0:
        return lines
    cost_mu = from_amount(estimated_cost.cost)
    init_mu = from_amount(estimated_cost.init_maintenance_cost)
    for h in hours:
        periods = math.ceil(h * 3600 / loop_seconds)
        total = cost_mu + price_bound.funding_sum_mu(init_mu, loop_seconds, bound, periods)
        note = "" if bound is not None else " (at the price of now; no limit)"
        lines.append(f"- Funding for {h} h ({periods} periods): {format_mu(total)}{note}")
    return lines
