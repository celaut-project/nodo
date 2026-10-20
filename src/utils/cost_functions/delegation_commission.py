"""What this node charges on top of a peer, when it delegates a service (#551).

A node that delegates does work for the client: it keeps a deposit on the peer, it
answers for the peer, and it carries the risk that the peer fails. Before #551 it
charged the client exactly the price of the peer, so it got nothing for this.

The commission applies to what the client consumes, never to the deposit:

* the start charge of the peer (the build, the part of ``EstimatedCost.cost`` above
  the balance that the quote asked for), once, with ``COMMISSION_MIN_MU`` as a floor;
* each maintenance tick: what the peer metered (``maintain_delegated_instances``).

The commission of the start is paid from ``Configuration.initial_mu``, as the build is
(#563). The client pays ``initial_mu`` and nothing else. This node keeps its commission
and sends the peer ``initial_mu`` less the commission. The peer takes its build from
that amount, and the rest is the balance of the child. So a quote of this node, as the
least ``initial_mu`` that starts the service, is the quote of the peer plus the
commission (``with_commission``).

The deposit goes back to the client when the instance stops, so a commission on it
would be a charge for nothing.

The traffic of a tunnel through this node is not here: ``tunnel_open_mu`` and
``net_mu_per_gib`` already charge it (``src/tunneling/rpc_tunnel.py``).
"""

from decimal import Decimal

from protos import celaut_pb2
from src.utils.config import ConfigManager

env_manager = ConfigManager()

PERCENT_KEY = "network.delegation.COMMISSION_PERCENT"
MIN_MU_KEY = "network.delegation.COMMISSION_MIN_MU"
DEFAULT_PERCENT = 5.0
DEFAULT_MIN_MU = 0


def commission_percent() -> float:
    """``network.delegation.COMMISSION_PERCENT``. A bad value reads as the default."""
    try:
        value = float(env_manager.get(PERCENT_KEY, DEFAULT_PERCENT))
    except (TypeError, ValueError):
        return DEFAULT_PERCENT
    return value if value >= 0 else DEFAULT_PERCENT


def commission_min_mu() -> int:
    """``network.delegation.COMMISSION_MIN_MU``. A bad value reads as the default."""
    try:
        value = int(env_manager.get(MIN_MU_KEY, DEFAULT_MIN_MU))
    except (TypeError, ValueError):
        return DEFAULT_MIN_MU
    return value if value >= 0 else DEFAULT_MIN_MU


def commission_on(amount_mu: int, with_minimum: bool = False) -> int:
    """The commission on ``amount_mu`` of consumption, rounded up.

    Rounded up for the same reason the tick rounds a peer's charge up: a floor would
    leave this node a fraction short on every charge, for the life of the instance.
    """
    amount = max(int(amount_mu), 0)
    # Integers only: MU amounts can be far beyond what a float, or a Decimal at its
    # default 28 digits, holds exactly. The percentage is read as an exact fraction.
    numerator, denominator = Decimal(str(commission_percent())).as_integer_ratio()
    commission = -(-amount * numerator // (denominator * 100))
    if with_minimum:
        commission = max(commission, commission_min_mu())
    return commission


def _mu(amount: celaut_pb2.Amount) -> int:
    return int(amount.n or 0)


def _amount(value: int) -> celaut_pb2.Amount:
    return celaut_pb2.Amount(n=str(int(value)))


def start_commission(peer_cost: celaut_pb2.EstimatedCost, initial_mu: int) -> tuple[int, int]:
    """The build of the peer and the commission of this node on it, both in local MU.

    ``peer_cost`` is the quote of the peer, already in local MU
    (``estimated_cost_for_local``). ``initial_mu`` is the balance that the quote asked
    for, in local MU. The quote is the build plus that balance, so the build is what is
    left above it. The commission is on the build only, with ``COMMISSION_MIN_MU`` as a
    floor.
    """
    build_mu = max(_mu(peer_cost.cost) - max(int(initial_mu), 0), 0)
    return build_mu, commission_on(build_mu, with_minimum=True)


def with_commission(
        peer_cost: celaut_pb2.EstimatedCost,
        initial_mu: int,
) -> celaut_pb2.EstimatedCost:
    """The price this node offers for a service that ``peer_cost`` says a peer runs.

    ``peer_cost`` is already in local MU (``estimated_cost_for_local``). ``initial_mu``
    is the balance that the quote of the peer asked for, in local MU: the build is what
    is left of the cost above it. The ``cost`` of the offer is the least ``initial_mu``
    that starts the service through this node: the least of the peer, plus the
    commission. The client pays that amount, and no other.
    """
    offer = celaut_pb2.EstimatedCost()
    offer.CopyFrom(peer_cost)
    _, start_commission_mu = start_commission(peer_cost, initial_mu)
    offer.cost.CopyFrom(_amount(_mu(peer_cost.cost) + start_commission_mu))
    for field in ("init_maintenance_cost", "max_maintenance_cost"):
        if peer_cost.HasField(field):
            value = _mu(getattr(peer_cost, field))
            getattr(offer, field).CopyFrom(_amount(value + commission_on(value)))
    return offer
