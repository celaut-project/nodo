"""How a start is funded: the build and the first runtime come out of ``initial_mu`` (#563).

The caller pays ``Configuration.initial_mu`` and nothing else. Every node takes the cost
of the build from the balance of the instance that it starts, and never from the account
of the caller:

    balance of the new instance = initial_mu - build

The maximum spend of the caller for the start is then ``initial_mu``, less what the
instance does not spend. The caller does not add the build to its own estimate.

``EstimatedCost.cost`` is what the start spends: the build, and the builds of the initial
children (``start_cost_mu``). It has no balance (#636, 9). The caller adds the balance
for the periods that it chooses (``price_bound.funding_sum_mu``). A start that carries no
``initial_mu`` gets ``cost`` plus the sum for ``deposits.DEFAULT_INITIAL_PERIODS``
periods (``default_balance_mu``).

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
from src.utils.cost_functions.execution_cost import (
    build_charge_mu,
    is_free,
    launch_charge_mu,
    prices,
)
from src.utils.cost_functions.price_bound import CURRENT, current_bound, funding_sum_mu
from src.utils.arch_guard import arch_from_tags
from src.utils.config import ConfigManager
from src.utils.verify import get_service_hex_main_hash, get_service_list_of_hashes
from src.virtualizers.interface import is_built, resolve_billable_resources

env_manager = ConfigManager()


class StartAmountTooSmall(Exception):
    """``initial_mu`` does not fund the build and the minimum runtime.

    A rejection of this kind is not a fault of anybody. It is not scored against a peer
    that sends it: the sender of the amount chose a figure that was too small.
    """


def start_build_mu(metadata: celaut.Metadata, scarcity: Optional[Dict[str, float]] = None) -> int:
    """The cost of the build that a start takes from ``initial_mu``, in MU.

    Zero while the node is free (``free_tier``) and for a service that is already built.
    The same figure that the quote puts in ``cost`` (``start_cost_mu``).
    """
    if is_free(scarcity):
        return 0
    return int(build_charge_mu(metadata=metadata))


def _workload_hash(workload: celaut.Service.Workload) -> Optional[str]:
    """The id of the service of a workload, in hex, or None if the workload names none.

    From ``dependency.hash`` (the configured hash type first), or calculated from a
    complete ``dependency.service``. A partial service names no service: its hash is
    not the hash of the real one.
    """
    if not workload.HasField("dependency"):
        return None
    dependency = workload.dependency
    hashes = [known for known in dependency.hash if known.value]
    if hashes:
        return get_service_hex_main_hash(other_hashes=hashes)
    if dependency.is_completed and dependency.HasField("service"):
        return get_service_hex_main_hash(
            other_hashes=get_service_list_of_hashes(dependency.service.SerializeToString())
        )
    return None


def child_start_cost_mu(
        workload: celaut.Service.Workload,
        scarcity: Optional[Dict[str, float]] = None,
) -> int:
    """What the start of the instances of one workload spends, apart from their balances.

    ``count`` times the build of the child: ``pricing.BUILD_MU`` when the child may need
    a build here, and 0 when the node knows that it does not, and while the node is free.

    | Hash of the service | Image          | Build in the quote |
    |---------------------|----------------|--------------------|
    | known               | built here     | 0                  |
    | known               | not built here | ``BUILD_MU``       |
    | not known           | any            | ``BUILD_MU``       |

    The hash is known when the workload gives a hash or a complete service. A workload
    that gives only resources has none, and the node cannot tell if the image is built:
    it assumes that it is not (#638). A quote that is too low cannot be recovered, because
    the parent runs out of balance before it starts the child. A quote that is too high
    can: the balance that the parent does not spend goes back to it when it stops. The
    price is flat, so it is exact when the image is not built.

    A service that declares its own ``initial_environment_workload`` adds the builds of
    its children, as its own ``cost`` does.

    No balance (#636, 9). Each child gets its balance in its own ``StartService``: the
    ``initial_mu`` that the parent sends, or with none the default of the node that runs
    it (``default_balance_mu``). The parent pays that ``child_start`` out of its own
    balance when it starts the child.
    """
    count = int(workload.count)
    if count <= 0:
        return 0
    service = workload.dependency.service
    service_hash = _workload_hash(workload)
    needs_build = True if not service_hash else not is_built(service_hash)
    build = prices().build_mu if needs_build and not is_free(scarcity) else 0
    grandchildren = initial_children_mu(service, scarcity=scarcity) \
        if service.initial_environment_workload else 0
    return int(count * (build + grandchildren))


def initial_children_mu(
        service: Optional[celaut.Service],
        scarcity: Optional[Dict[str, float]] = None,
) -> int:
    """The builds of the initial children of a service. ``EstimatedCost.cost`` includes it.

    ``Service.initial_environment_workload`` is an AND of clauses, a clause an OR of
    groups, and a group an AND of workloads that run at the same time. The cost of a
    group is the sum of its workloads (``child_start_cost_mu``). The cost of a clause
    is the highest cost of its groups, because the parent chooses the group at run time.
    The total is the sum of the clauses.

    The quote does not probe peers: it uses all the groups of a clause, not only the
    ones that fit now, and the price of this node. This can over-quote. An over-quote
    is safe: the balance that the instance does not spend goes back to the parent when
    it stops. ``possible_environment_workload`` has no cost.
    """
    if service is None:
        return 0
    total = 0
    for clause in service.initial_environment_workload:
        total += max(
            (sum(child_start_cost_mu(workload, scarcity) for workload in group.workloads)
             for group in clause.any_of),
            default=0,
        )
    return int(total)


def start_cost_mu(
        metadata: celaut.Metadata,
        service: Optional[celaut.Service] = None,
        scarcity: Optional[Dict[str, float]] = None,
) -> int:
    """``EstimatedCost.cost``: what the start spends, with no balance (#636, 9).

    The build of the service (``start_build_mu``) and the builds of its initial children
    (``initial_children_mu``). Both are 0 while the node is free, so a free node quotes
    a cost of 0, and the funding sum asks for the balance as for any start (#636, 2).
    """
    return int(start_build_mu(metadata=metadata, scarcity=scarcity)
               + initial_children_mu(service, scarcity=scarcity))


def default_initial_periods() -> int:
    """``deposits.DEFAULT_INITIAL_PERIODS``: how many maintenance periods the default
    balance of a start funds. At least 1."""
    return max(int(ConfigManager().get("deposits.DEFAULT_INITIAL_PERIODS", 360)), 1)


def periods_funding_mu(
        system_resources: Optional[celaut.Sysresources],
        periods: int,
        service_hash: Optional[str] = None,
        arch: Optional[str] = None,
        service: Optional[celaut.Service] = None,
        scarcity: Optional[Dict[str, float]] = None,
        bound=CURRENT,
) -> int:
    """The balance that funds ``periods`` maintenance periods of an instance launched now.

    ``price_bound.funding_sum_mu`` of the maintenance of one period
    (``MANAGER_ITERATION_TIME``) with ``system_resources``, at the price of a launch now
    (``launch_charge_mu``, the ``init_maintenance_cost`` of a quote). It is the formula
    that a client uses with a quote, and the ticks of the instance take ``k`` from the same
    function (``price_bound.tick_period_index``, #636, 6).

    Priced for what the instance will hold, not for what its manifest asks
    (``resolve_billable_resources``): the ticks charge the resolved row. ``arch`` selects
    the memory price when the operator prices memory per arch. A free node prices at the
    anchor of a launch while free (1x), not at 0: its ticks charge 0 while it stays free,
    and up to the bound over that anchor when it leaves the free state. ``scarcity`` is
    the load sample of the launch; omitted, it is read here. ``bound`` is the bound of
    the launch (None for no bound); omitted, the bound of now is read here.
    """
    if system_resources is None or int(periods) <= 0:
        return 0
    if bound is CURRENT:
        bound = current_bound()
    loop_seconds = int(ConfigManager().get("MANAGER_ITERATION_TIME"))
    init_mu = launch_charge_mu(
        system_resources=resolve_billable_resources(system_resources, service_hash, service=service),
        seconds=loop_seconds,
        scarcity=scarcity,
        arch=arch,
    )
    return int(funding_sum_mu(init_mu, loop_seconds, bound, int(periods)))


def default_balance_mu(
        system_resources: Optional[celaut.Sysresources],
        service_hash: Optional[str] = None,
        arch: Optional[str] = None,
        service: Optional[celaut.Service] = None,
        scarcity: Optional[Dict[str, float]] = None,
        bound=CURRENT,
) -> int:
    """The balance of a start that carries no ``initial_mu``: ``periods_funding_mu`` for
    ``deposits.DEFAULT_INITIAL_PERIODS`` periods (#636, 9).

    The node funds such a start with ``EstimatedCost.cost`` (``start_cost_mu``) plus this
    figure. It has no balance for the children: each child gets its own default in its
    own ``StartService``, and the parent pays it out of this balance.
    """
    return periods_funding_mu(
        system_resources, default_initial_periods(), service_hash=service_hash, arch=arch,
        service=service, scarcity=scarcity, bound=bound,
    )


def minimum_runtime_mu(
        resources: celaut.Service.Container.Resources,
        service_hash: Optional[str] = None,
        arch: Optional[str] = None,
        service: Optional[celaut.Service] = None,
        scarcity: Optional[Dict[str, float]] = None,
        bound=CURRENT,
) -> int:
    """The least balance that an instance holds after the build: one maintenance period.

    ``periods_funding_mu`` for one period with the resources at the start: the launch
    price times ``(1 + v)^k(1)`` (#636), the most that the first tick of the instance can
    charge. With ``pricing.PRICE_BOUNDED`` false, it is the launch price. So a client
    that funds ``cost`` plus the funding sum of one period passes. ``bound`` is the
    bound of the launch, read once by the launcher; omitted, it is read here.
    """
    if not resources.HasField("at_init"):
        return 0
    return periods_funding_mu(
        resources.at_init, 1, service_hash=service_hash, arch=arch, service=service,
        scarcity=scarcity, bound=bound,
    )


def underfunded_reason(initial_mu: int, build_mu: int, minimum_mu: int, children_mu: int) -> Optional[str]:
    """Why ``initial_mu`` does not fund the initial children of a service, or None (#538).

    A service pays the ``child_start`` of its initial children out of its own balance.
    So the balance after the build must pay ``children_mu``, the builds of the children
    (``initial_children_mu``), and one maintenance period of the service
    (``minimum_mu``). If not, the parent starts and then fails to start its children in
    a loop (#628). The balances of the children are not in ``children_mu`` (#636, 9):
    the parent pays them out of the periods that it was funded for. A service with no
    initial children to build is not checked here:
    ``balance_after_build`` applies the rule of the build and the period.
    """
    initial_mu, build_mu = int(initial_mu), int(build_mu)
    minimum_mu, children_mu = int(minimum_mu), int(children_mu)
    if children_mu <= 0:
        return None
    least = build_mu + children_mu + minimum_mu
    if initial_mu >= least:
        return None
    return (
        f"initial_mu {initial_mu} MU leaves {max(initial_mu - build_mu, 0)} MU after the "
        f"build of {build_mu} MU. The builds of the initial children cost {children_mu} MU "
        f"and one maintenance period costs {minimum_mu} MU. "
        f"The least initial_mu is {least} MU."
    )


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
