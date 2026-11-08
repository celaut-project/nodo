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
from src.utils.cost_functions.execution_cost import (
    build_charge_mu,
    is_free,
    prices,
    worst_case_charge_mu,
)
from src.utils.cost_functions.general_cost_functions import compute_worst_case_maintenance_cost
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
    The same figure that the quote adds to the balance (``compute_start_service_cost``).
    """
    if is_free(scarcity):
        return 0
    return int(build_charge_mu(metadata=metadata))


def _group_hash(group: "celaut.Service.Workload") -> Optional[str]:
    """The SHA3-256 of the service of a workload group, in hex, if the group names one."""
    if not group.HasField("dependency"):
        return None
    for known in group.dependency.hash:
        if known.value:
            return known.value.hex()
    return None


def descendant_funding_mu(
        service: celaut.Service,
        arch: Optional[str] = None,
        scarcity: Optional[Dict[str, float]] = None,
) -> int:
    """The MU that a service asks to be funded with for the descendants that it declares.

    ``Service.possible_environment_workload`` lists scenarios of descendants that the
    service can start at the same time. A service pays for them out of its own balance
    (each start takes its ``initial_mu`` from the father), so the balance that a start
    funds it with has to cover the most that one scenario can need. The scenarios do not
    add up, so the figure is the dearest one.

    A scenario costs, for each group of identical instances:

    * the runtime of ``count`` instances with the resources of the group, for the same
      ``deposits.INITIAL_RUNTIME_HOURS`` as the service itself and at the same worst
      case of the price (``worst_case_charge_mu``), and
    * one build (``pricing.BUILD_MU``) for the group, whatever its ``count``: identical
      instances are built once. The build is left out when the group names a service
      (``dependency.hash``) that is built on this node already, and while the node is
      free. A group that gives only resources names no service, so its build is counted.

    A service that declares no workload costs 0 here, and a group with ``count`` 0 too.
    The figure is the same one for the quote, the funding of the instance and the quote
    that is asked of a peer: ``manager.default_initial_balance`` adds it in the three.
    """
    if not service.possible_environment_workload:
        return 0
    hours = float(env_manager.get("deposits.INITIAL_RUNTIME_HOURS", 1.0))
    if hours <= 0:
        return 0

    free = is_free(scarcity)
    best = 0
    for scenario in service.possible_environment_workload:
        total = 0
        for group in scenario.workloads:
            count = int(group.count)
            if count <= 0:
                continue
            service_hash = _group_hash(group)
            runtime = worst_case_charge_mu(
                system_resources=resolve_billable_resources(group.resources, service_hash),
                seconds=hours * 3600,
                scarcity=scarcity,
                arch=arch,
            )
            total += count * runtime
            if not free and not (service_hash and is_built(service_hash)):
                total += prices().build_mu
        best = max(best, total)
    return int(best)


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
    """What the parent pays for the ``child_start`` of the instances of one workload.

    ``count`` times the least ``initial_mu`` that starts one child with the default
    balance of this node: the build of the child plus
    ``manager.default_initial_balance`` for the ``at_init`` of its service. These are
    the figures of a real start (``compute_start_service_cost``), so the quote and the
    charge come from the same price. The build is 0 when the workload names no service
    (neither a hash nor a complete service), when the service is built here already,
    and while the node is free. Free, the start costs nothing at all.

    The resources are the ones of ``dependency.service.container.resources``. A
    workload with no resources is priced at the floors of this node, as a real start of
    a service that declares none. A service that declares its own
    ``initial_environment_workload`` adds the cost of its children, as its default
    balance does.
    """
    from src.manager.manager import default_initial_balance

    count = int(workload.count)
    if count <= 0 or is_free(scarcity):
        return 0
    service = workload.dependency.service
    service_hash = _workload_hash(workload)
    balance = default_initial_balance(
        system_resources=service.container.resources.at_init,
        service_hash=service_hash,
        arch=arch_from_tags(service.container.architecture.tags),
        scarcity=scarcity,
        service=service,
    )
    build = prices().build_mu if service_hash and not is_built(service_hash) else 0
    return int(count * (build + balance))


def children_initial_cost_mu(
        service: Optional[celaut.Service],
        scarcity: Optional[Dict[str, float]] = None,
) -> int:
    """``EstimatedCost.children_initial_cost``: what a service pays to start its initial children.

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
