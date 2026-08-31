from typing import Dict, Optional

from protos import celaut_pb2 as celaut
from src.utils.config import ConfigManager
from src.utils.cost_functions.execution_cost import (
    build_charge_mu,
    is_free,
    maintenance_charge_mu,
)
from src.utils.monetary import HOUR_SECONDS, prices

env_manager = ConfigManager()

# Keys of the rate map this node advertises to peers. Names are part of the wire
# contract: a peer reads them out of Peer.mu_per_call (see gateway.utils._build_peer),
# so renaming one silently drops it for everybody who already knows the old name.
#
# Every rate is in MU. What an MU is worth travels alongside them in
# `Peer.payment_contracts` as `ContractRate.mu_per_unit`, so a peer reading a rate can
# convert it into real money and compare two nodes. The rates this map used to carry
# were denominated in "gas", which nothing anywhere declared a rate for, so they meant
# nothing to the node reading them.
RATE_RAM_PER_GIB_SECOND = "ram_mu_per_gib_second"
# Per-architecture memory rate. The suffix is the canonical arch tag the rest of the
# node names architectures by, verbatim -- `ram_mu_per_gib_second:linux/amd64` -- so a
# reader that knows a service's arch can look its rate up without a second naming
# convention, and one that does not still reads the un-suffixed key above.
#
# It rides in the SAME `Peer.mu_per_call` map every other rate does, which is why this
# needs no protobuf change: the map is <string, Amount>, its keys are already an open
# vocabulary, and a peer running an older nodo simply does not find the suffixed key
# and falls back to the scalar one -- the behaviour it has today. See
# `node_advertised_rates`.
RATE_RAM_PER_GIB_SECOND_ARCH_PREFIX = f"{RATE_RAM_PER_GIB_SECOND}:"
RATE_CPU_PER_VCPU_SECOND = "cpu_mu_per_vcpu_second"
RATE_DISK_PER_GIB_SECOND = "disk_mu_per_gib_second"
RATE_NET_PER_GIB = "net_mu_per_gib"
RATE_BUILD = "build_mu"
RATE_TUNNEL_OPEN = "tunnel_open_mu"
RATE_MODIFY_RESOURCES = "modify_resources_mu"
RATE_SCARCITY_MAX_MULTIPLIER = "scarcity_max_multiplier"


def compute_start_service_cost(metadata: celaut.Metadata, initial_balance_mu: int) -> int:
    """Total MU to start an instance: the one-off build, plus the balance it starts with.

    The build is the only thing the *start* charges for. The runtime window is priced
    once, as ``initial_balance_mu`` -- the balance the instance holds and the maintenance
    ticks then spend.

    This used to add ``seconds`` of occupancy on top of that balance, billing the same
    window twice: on an idle node the documented instance cost 0.0205 ERG to start where
    0.01525 ERG buys the build and funds the hour, and the difference bought nothing. On a
    loaded node it was worse, because that occupancy carried the live scarcity surcharge —
    so the price quoted to start swung with whatever else the machine was doing.

    Takes no resources for the same reason: nothing here is priced per resource any more.
    """
    if is_free():
        return 0
    return int(build_charge_mu(metadata=metadata) + initial_balance_mu)


def compute_maintenance_cost(
    system_resources: celaut.Sysresources,
    seconds: float,
    scarcity: Optional[Dict[str, float]] = None,
    arch: Optional[str] = None,
) -> int:
    """MU owed for holding `system_resources` for `seconds`.

    `scarcity` lets a caller sweeping many instances read system load once and price
    them all against the same snapshot; omitted, each call samples it itself.

    `arch` selects the per-architecture memory price when one is configured; omitted,
    the node's scalar memory price applies.
    """
    return maintenance_charge_mu(
        system_resources=system_resources, seconds=seconds, scarcity=scarcity, arch=arch
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
        RATE_MODIFY_RESOURCES: p.modify_resources_mu,
        RATE_SCARCITY_MAX_MULTIPLIER: p.scarcity_max_multiplier,
    }

    # A node that prices memory per architecture advertises one entry per priced arch,
    # alongside (never instead of) the scalar rate: a peer that does not know about
    # per-arch pricing keeps reading exactly what it reads today. A peer that does can
    # find the rate for the arch it wants to run and, when the node prices that arch
    # differently, know before asking for a quote.
    #
    # Only architectures the operator actually priced appear. Emitting one per
    # supported arch, equal to the scalar, would advertise a per-arch policy the node
    # does not have and grow every announcement for nothing.
    for arch, price_mu_per_gib_hour in p.ram_mu_per_gib_hour_by_arch.items():
        rates[f"{RATE_RAM_PER_GIB_SECOND_ARCH_PREFIX}{arch}"] = (
            int(price_mu_per_gib_hour) // HOUR_SECONDS
        )

    return {key: int(value) for key, value in rates.items() if int(value) > 0}
