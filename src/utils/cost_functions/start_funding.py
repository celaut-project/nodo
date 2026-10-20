"""How a start is funded: the build and the first runtime come out of ``initial_mu`` (#563).

The caller pays ``Configuration.initial_mu`` and nothing else. Every node takes the cost
of the build from the balance of the instance that it starts, and never from the account
of the caller:

    balance of the new instance = initial_mu - build

The maximum spend of the caller for the start is then ``initial_mu``, less what the
instance does not spend. The caller does not add the build to its own estimate.

Two rules keep the start from a trap for the caller:

* The build must fit in ``initial_mu``. If it does not, the node rejects the start and
  charges nothing.
* The balance that is left must pay at least one maintenance period with the resources
  at the start (``minimum_runtime_mu``). Without this rule, a caller could pay the build
  and get an instance that dies at the first tick.

This module is the one place that states both rules. The quote, the launch on this node
and the check before a delegation use it, so they cannot disagree.
"""

from typing import Dict, Optional

from protos import celaut_pb2 as celaut
from src.utils.cost_functions.execution_cost import build_charge_mu, is_free
from src.utils.cost_functions.general_cost_functions import compute_worst_case_maintenance_cost
from src.utils.config import ConfigManager
from src.virtualizers.interface import resolve_billable_resources

env_manager = ConfigManager()


class StartAmountTooSmall(Exception):
    """``initial_mu`` does not fund the build and the minimum runtime.

    A rejection of this kind is not a fault of anybody. It is not scored against a peer
    that sends it: the sender of the amount chose a figure that was too small.
    """


def start_build_mu(metadata: celaut.Metadata, scarcity: Optional[Dict[str, float]] = None) -> int:
    """The cost of the build that a start takes from ``initial_mu``, in MU.

    Zero while the node is free (``free_tier``) and for a service that is already built.
    The same figure that the quote adds to the balance (``compute_start_service_cost``).
    """
    if is_free(scarcity):
        return 0
    return int(build_charge_mu(metadata=metadata))


def minimum_runtime_mu(
        resources: celaut.Service.Container.Resources,
        service_hash: Optional[str] = None,
        arch: Optional[str] = None,
        service: Optional[celaut.Service] = None,
        scarcity: Optional[Dict[str, float]] = None,
) -> int:
    """The least balance that an instance holds after the build: one maintenance period.

    It is one period (``MANAGER_ITERATION_TIME``) with the resources at the start, at the
    worst case that the price can reach. With ``pricing.MAX_PRICE_VOLATILITY`` on, that
    is the launch price times the bound, so no tick of the instance can charge more than
    this figure. With the bound off, it is the price at the current load. It is the same
    function that prices ``EstimatedCost.max_maintenance_cost``.
    """
    if not resources.HasField("at_init"):
        return 0
    return int(compute_worst_case_maintenance_cost(
        system_resources=resolve_billable_resources(resources.at_init, service_hash, service=service),
        seconds=env_manager.get("MANAGER_ITERATION_TIME"),
        scarcity=scarcity,
        arch=arch,
    ))


def balance_after_build(initial_mu: int, build_mu: int, minimum_mu: int) -> int:
    """The balance of the new instance, or raise ``StartAmountTooSmall``.

    ``initial_mu - build_mu`` when it is at least ``minimum_mu``. The message says which
    of the two rules the amount breaks, so the operator and the caller can fix it.
    """
    initial_mu, build_mu, minimum_mu = int(initial_mu), int(build_mu), int(minimum_mu)
    if initial_mu < build_mu:
        raise StartAmountTooSmall(
            f"initial_mu {initial_mu} MU does not cover the build of the service, "
            f"{build_mu} MU."
        )
    balance = initial_mu - build_mu
    if balance < minimum_mu:
        raise StartAmountTooSmall(
            f"initial_mu {initial_mu} MU leaves {balance} MU after the build of {build_mu} MU. "
            f"One maintenance period costs {minimum_mu} MU. "
            f"The least initial_mu is {build_mu + minimum_mu} MU."
        )
    return balance
