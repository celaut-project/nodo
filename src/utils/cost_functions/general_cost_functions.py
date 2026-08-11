from typing import Dict, Optional

from protos import celaut_pb2 as celaut, celaut_pb2
from src.utils.config import ConfigManager
from src.utils.cost_functions.execution_cost import (
    build_charge_mu,
    maintenance_charge_mu,
    start_charge_mu,
)
from src.utils.monetary import HOUR_SECONDS, prices

env_manager = ConfigManager()

# Keys of the rate map this node advertises to peers. Names are part of the wire
# contract: a peer reads them out of Service.Api.Slot.mu_per_call, so renaming one
# silently drops it for everybody who already knows the old name.
#
# Every rate is in MU. What an MU is worth travels alongside them in
# `Peer.payment_contracts` as `ContractRate.mu_per_unit`, so a peer reading a rate can
# convert it into real money and compare two nodes. The rates this map used to carry
# were denominated in "gas", which nothing anywhere declared a rate for, so they meant
# nothing to the node reading them.
RATE_RAM_PER_GIB_SECOND = "ram_mu_per_gib_second"
RATE_CPU_PER_VCPU_SECOND = "cpu_mu_per_vcpu_second"
RATE_DISK_PER_GIB_SECOND = "disk_mu_per_gib_second"
RATE_NET_PER_GIB = "net_mu_per_gib"
RATE_BUILD = "build_mu"
RATE_TUNNEL_OPEN = "tunnel_open_mu"
RATE_SCARCITY_MAX_MULTIPLIER = "scarcity_max_multiplier"


def compute_start_service_cost(
    metadata: celaut.Metadata,
    initial_balance_mu: int,
    resource: celaut_pb2.Service.Container.Resources,
    seconds: float,
) -> int:
    """Total MU to start an instance: the one-off charges plus the balance it starts with."""
    return int(
        start_charge_mu(metadata=metadata, system_resources=resource.at_init, seconds=seconds)
        + initial_balance_mu
    )


def compute_maintenance_cost(
    system_resources: celaut.Sysresources,
    seconds: float,
    scarcity: Optional[Dict[str, float]] = None,
) -> int:
    """MU owed for holding `system_resources` for `seconds`.

    `scarcity` lets a caller sweeping many instances read system load once and price
    them all against the same snapshot; omitted, each call samples it itself.
    """
    return maintenance_charge_mu(
        system_resources=system_resources, seconds=seconds, scarcity=scarcity
    )


def compute_build_cost(metadata: celaut.Metadata) -> int:
    return build_charge_mu(metadata=metadata)


def node_advertised_rates() -> Dict[str, int]:
    """This node's prices, for peers to read before negotiating anything.

    These are the charges a peer cannot discover any other way. The cost of a *specific
    service* is not here on purpose -- that comes from ``GetServiceEstimatedCost``, which
    prices the actual resources requested against current load.

    Every rate is a base price, before the scarcity surcharge: what a peer pays is this
    figure multiplied by between 1 and ``scarcity_max_multiplier``, depending on how
    contended that particular resource is when it asks. Advertising the ceiling alongside
    the base is what lets a peer bound its exposure without pretending the price is fixed.

    Rates are per second (not per hour) so they compose with any measurement window, and
    a rate of zero is omitted rather than advertised as 0, so "free" is never claimed by
    accident.
    """
    p = prices()

    # Per-GiB-hour prices become per-GiB-second. Integer division truncates, so a price
    # under 3600 MU per GiB-hour advertises as 0 and is therefore omitted -- correct, in
    # that a peer cannot be quoted a per-second price that rounds to nothing.
    rates = {
        RATE_RAM_PER_GIB_SECOND: p.ram_mu_per_gib_hour // HOUR_SECONDS,
        RATE_CPU_PER_VCPU_SECOND: p.cpu_mu_per_vcpu_hour // HOUR_SECONDS,
        RATE_DISK_PER_GIB_SECOND: p.disk_mu_per_gib_hour // HOUR_SECONDS,
        RATE_NET_PER_GIB: p.net_mu_per_gib,
        RATE_BUILD: p.build_mu,
        RATE_TUNNEL_OPEN: p.tunnel_open_mu,
        RATE_SCARCITY_MAX_MULTIPLIER: p.scarcity_max_multiplier,
    }
    return {key: int(value) for key, value in rates.items() if int(value) > 0}
