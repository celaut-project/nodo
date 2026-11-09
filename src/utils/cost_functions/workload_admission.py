"""Admission check for the descendant workloads that a service declares.

`service.json` declares the descendant workloads a service may spawn (issues #163 and
#538); the packer serializes that declaration without interpreting it. This module is
where it is interpreted, at launch.

The declaration is in conjunctive normal form, in two fields with one shape:

* ``Service.initial_environment_workload`` -- the load the service needs to start. It
  is checked here and priced by ``start_funding.children_initial_cost_mu``.
* ``Service.possible_environment_workload`` -- the load it can ask for later. It is
  checked here only.

Each field is an AND of clauses, a clause is an OR of groups, and a group is an AND of
workloads that run at the same time:

* A workload fits if its shape can run *somewhere* -- locally, or on at least one known
  peer. The shape is the one of its dependency service: the limits of
  ``container.resources.at_most`` (``at_init``'s when there is no ``at_most``), the
  minimum benchmark of ``at_init.benchmark`` and the architecture of
  ``container.architecture``. ``at_most.benchmark`` has no meaning and is ignored. A
  workload with no resources, or with ``count`` 0, always fits.
* A group fits if all its workloads fit.
* A clause fits if at least one of its groups fits. The check stops at the first group
  that fits. A clause with no group declares nothing and fits.
* The service is admitted if every clause of both fields fits.

This generalizes the exact same boolean admission gate
`resource_availability.get_resource_availability` already runs for a service's own
`at_most` resources, applied instead to each descendant workload.

Every limit a workload declares is checked, not only `mem_limit`: memory and disk
against what is free right now, a CPU quota against the number of cores the host has
at all (a quota is a share of time, so refusing on instantaneous load would make
admission flap), and `blkio_weight` against the range cgroups accept. See
`resource_availability._sysreq_shortfalls`.

Two policies govern the rest, both in `config.yaml` under `workload_admission` and both
read per call:

* `POLICY` -- how much to probe. `fail_fast` (default) stops at the first clause that
  fits nowhere, and inside a group at the first workload that fits nowhere; `full`
  probes every workload so the message names all of them. This never changes *what* is
  admitted.
* `ON_UNSATISFIABLE` -- what a clause that fits nowhere means. `reject` (default)
  refuses the launch; `warn` logs and lets it through, for an operator who would rather
  risk it than lose a launch to a momentary reading.

What this does NOT do, and should not be read as doing: it does not reserve capacity,
and it does not prove that `count` concurrent instances of a workload, or the workloads
of one group, could all run at once (locally, or spread across peers) -- there is no
capacity ledger anywhere in this codebase that tracks that, for real services or for
these hypothetical ones. It only proves existence: that a shape this big fits
*somewhere* right now, exactly as confident as the single-service admission check it
reuses and no more.
"""
from typing import Final, List, Optional, Tuple

from protos import celaut_pb2 as celaut
from src.utils import logger as log
from src.utils.cost_functions.resource_availability import get_architecture_availability
from src.utils.config import ConfigManager

env_manager = ConfigManager()

EXTERNAL_COST_TIMEOUT = env_manager.get("EXTERNAL_COST_TIMEOUT")

# How much of the declaration to probe before answering. This is *only* about how much
# is probed, never about what is admitted: every declared clause must fit, under both
# policies. What differs is whether the node keeps asking once the answer is settled.
#
#   fail_fast -- stop at the first clause that fits nowhere (and, inside a group, at the
#                first workload that fits nowhere). The service is refused the
#                moment one clause fails, so every probe after that one buys nothing but
#                a longer error message, and each of them is a gRPC round-trip to every
#                known peer. The default: the rejection path must not be the expensive
#                one.
#   full      -- probe every workload and report all of them. Worth its cost when a
#                declaration is being debugged and "which of my workloads do not fit?" is
#                the actual question, not "may this launch proceed?".
PROBE_ALL_GROUPS: Final[str] = "full"
PROBE_UNTIL_FIRST_FAILURE: Final[str] = "fail_fast"
_DEFAULT_PROBE_POLICY: Final[str] = PROBE_UNTIL_FIRST_FAILURE

# What a clause that fits nowhere means for the launch.
#
#   reject -- refuse it (the default). The declaration says these descendants may be
#             spawned; if there is nowhere to spawn them, starting the parent only
#             defers the failure to a worse moment.
#   warn   -- log it and launch anyway. This check reads capacity *right now* and
#             holds nothing, so on a busy node it can refuse a service that would have
#             run perfectly well a second later; an operator who would rather take
#             that chance than lose the launch says so here.
ON_UNSATISFIABLE_REJECT: Final[str] = "reject"
ON_UNSATISFIABLE_WARN: Final[str] = "warn"
_DEFAULT_ON_UNSATISFIABLE: Final[str] = ON_UNSATISFIABLE_REJECT

# What a start whose `initial_mu` cannot pay the initial children means (#538).
#
#   reject -- refuse it (the default). The parent pays the `child_start` of each child
#             of its `initial_environment_workload` out of its balance. Started with too
#             little, it fails to start them and retries in a loop, and the application
#             never becomes ready (#628). Refused here, it fails at once, with the
#             figure that it needs.
#   warn   -- log it and launch anyway, for a service that starts its children later or
#             that is topped up by hand.
ON_UNDERFUNDED_REJECT: Final[str] = "reject"
ON_UNDERFUNDED_WARN: Final[str] = "warn"
_DEFAULT_ON_UNDERFUNDED: Final[str] = ON_UNDERFUNDED_REJECT


def _policy(key: str, valid: Tuple[str, ...], default: str) -> str:
    """One of ``valid``, read per call rather than captured at import.

    An unrecognised value falls back to the default and says so: silently applying
    something other than what the config asks for is how a node ends up admitting
    work its operator believed it was refusing.
    """
    try:
        configured = str(env_manager.get(key, default) or default).strip().lower()
    except Exception:
        return default
    if configured not in valid:
        log.LOGGER(
            f"[WORKLOAD ADMISSION] {key} is {configured!r}, which is not one of "
            f"{', '.join(valid)}; using {default!r}."
        )
        return default
    return configured


def _probe_policy() -> str:
    return _policy(
        "workload_admission.POLICY",
        (PROBE_ALL_GROUPS, PROBE_UNTIL_FIRST_FAILURE),
        _DEFAULT_PROBE_POLICY,
    )


def _on_unsatisfiable() -> str:
    return _policy(
        "workload_admission.ON_UNSATISFIABLE",
        (ON_UNSATISFIABLE_REJECT, ON_UNSATISFIABLE_WARN),
        _DEFAULT_ON_UNSATISFIABLE,
    )


def _on_underfunded() -> str:
    return _policy(
        "workload_admission.ON_UNDERFUNDED",
        (ON_UNDERFUNDED_REJECT, ON_UNDERFUNDED_WARN),
        _DEFAULT_ON_UNDERFUNDED,
    )


def _timeout() -> Optional[int]:
    try:
        timeout = int(EXTERNAL_COST_TIMEOUT)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def check_resource_availability_on_peer(
        peer_id: str,
        request: celaut.ArchitectureResources,
) -> Optional[bool]:
    """Ask exactly one peer whether it could run an instance of this architecture
    shaped like `request` right now. None means the peer could not be asked
    (unreachable, timed out, running a version without the RPC, or holding an
    address that turns out not to prove its identity) -- not a "no".

    An answer already obtained for this very question is reused for
    `network.QUERY_CACHE_AVAILABILITY_TTL_SECONDS` (#456), so probes for the same shape
    do not ask the same peer again. The question carries no recursion token: the peer
    answers from its own machine and never passes it on, and what it remembers is keyed
    by content (`src/utils/tools/query_cache.py`).
    """
    # Imported lazily: this is the only place in the module that talks to a
    # peer, and keeping the rest importable without bee_rpc/grpc installed is
    # what lets evaluate_possible_environment_workloads' own logic be unit
    # tested on a host that has neither (see tests/test_workload_admission.py).
    # BeeClient itself imports bee_rpc at module scope, so it has to stay out of
    # this file's own top-level imports for the same reason.
    from src.identity.grpc_transport import peer_channel
    from src.manager.manager import get_client_id_on_other_peer
    from src.utils.bee_client import BeeClient
    from src.utils.tools.query_cache import HIT, QueryCache, availability_ttl, canonical_key

    try:
        ttl = availability_ttl()
        key = canonical_key("peer-availability", peer_id, request) if ttl > 0 else None
        if key is not None:
            status, remembered = QueryCache().recall(key)
            if status == HIT:
                return remembered
        response = BeeClient.get_resource_availability(
            peer_channel(peer_id=peer_id), request, timeout=_timeout(),
            client_id=get_client_id_on_other_peer(peer_id=peer_id),
        )
        if key is not None:
            QueryCache().remember(key, response.can_execute, ttl)
        return response.can_execute
    except Exception as e:
        log.LOGGER(f"Could not check resource availability on peer {peer_id}: {e}")
        return None


WORKLOAD_FIELDS: Final[Tuple[str, ...]] = (
    "initial_environment_workload",
    "possible_environment_workload",
)


def group_request(workload: celaut.Service.Workload) -> celaut.ArchitectureResources:
    """What one descendant workload asks of whoever would run it.

    The shape is the one of the dependency service (#538): the limits of
    ``container.resources.at_most`` -- what each descendant may grow to -- and the
    minimum benchmark of ``at_init.benchmark`` (see Sysresources.benchmark).
    ``at_most.benchmark`` has no meaning and is dropped. A service that gives no
    ``at_most`` is limited by its ``at_init``. The architecture is the dependency
    service's, or left empty -- "whatever you natively run" -- when it does not say.
    """
    request = celaut.ArchitectureResources()
    if not (workload.HasField("dependency") and workload.dependency.HasField("service")):
        return request
    container = workload.dependency.service.container
    request.architecture.CopyFrom(container.architecture)
    resources = container.resources
    limits = resources.at_most if resources.HasField("at_most") else resources.at_init
    request.resources.CopyFrom(limits)
    del request.resources.benchmark[:]
    request.resources.benchmark.extend(resources.at_init.benchmark)
    return request


def _local_resource_availability(request: celaut.ArchitectureResources) -> dict:
    """Whether this host could take one instance of ``request``'s architecture and shape.

    Its own function rather than a call inlined into
    :func:`_workload_group_is_satisfiable` so a test can replace exactly this step and
    drive the peer half of the decision on its own.
    """
    return get_architecture_availability(request)


def _workload_group_is_satisfiable(
        request: celaut.ArchitectureResources,
        ignore_network: Optional[str],
) -> bool:
    from src.utils.cost_functions.architecture_resources import executes_locally

    # This host counts only while the node runs work on it. With
    # `network.EXECUTE_LOCALLY: false` the room it has is not on offer, and a group
    # admitted on its strength would be refused at launch for want of a local
    # candidate.
    if executes_locally() and _local_resource_availability(request)["can_execute"]:
        return True

    if not env_manager.get("network.DELEGATE_EXECUTION", True):
        return False

    from src.utils.utils import peers_id_iterator  # see check_resource_availability_on_peer

    from src.utils.arch_guard import arch_from_tags
    from src.utils.cost_functions.architecture_resources import should_skip_peer

    arch = arch_from_tags(request.architecture.tags)
    for peer_id in peers_id_iterator(ignore_network=ignore_network):
        # Skipped when the peer's own announcement already rules it out (#454, #459).
        if should_skip_peer(peer_id, arch, request.resources, "GetResourceAvailability"):
            continue
        if check_resource_availability_on_peer(peer_id, request) is True:
            return True
    return False


def _declared_limits(resources: celaut.Sysresources) -> str:
    """The limits a group actually declares, for the message that refuses it.

    Read off the descriptor rather than named one at a time. A hand-written list
    describes a group by a fixed few fields whichever one refused it, so a group
    turned away over `blkio_weight` or `cpu_period` reads as a `mem_limit`, a
    `disk_space` and a `cpu_quota` that all look perfectly fine, and a limit added to
    Sysresources goes unmentioned until someone remembers this string. `ListFields`
    lists the fields that are set, in field-number order, so every declared limit is
    named and an undeclared one stays out rather than showing up as a `0` the
    operator has to discount.
    """
    declared = ", ".join(f"{field.name}={value}" for field, value in resources.ListFields())
    return declared or "no declared limits"


def _unsatisfiable_workloads(
        group: celaut.Service.Group,
        path: str,
        ignore_network: Optional[str],
        *,
        stop_at_first: bool,
) -> List[str]:
    """Every workload of ``group`` that fits nowhere, described one per entry."""
    failures: List[str] = []
    for workload_index, workload in enumerate(group.workloads):
        request = group_request(workload)
        if workload.count == 0 or not request.resources.ListFields():
            continue  # No resource requirement declared; trivially satisfiable.

        if _workload_group_is_satisfiable(request, ignore_network):
            continue

        failures.append(
            f"{path}.workloads[{workload_index}] "
            f"(count={workload.count}, {_declared_limits(request.resources)})"
        )
        if stop_at_first:
            break
    return failures


def _unsatisfiable_clauses(
        service: celaut.Service,
        ignore_network: Optional[str],
        *,
        stop_at_first: bool,
) -> List[str]:
    """Every declared clause that has no group that fits, described one per entry.

    A clause is an OR: the check of a clause stops at its first group that fits.
    ``stop_at_first`` returns as soon as one clause fails, and stops a group at its
    first workload that fits nowhere. It changes nothing about which services are
    admitted -- every clause still has to fit -- only how many peers are asked once the
    answer is already settled.
    """
    failures: List[str] = []

    for field in WORKLOAD_FIELDS:
        for clause_index, clause in enumerate(getattr(service, field)):
            clause_path = f"{field}[{clause_index}]"
            refused: List[str] = []
            for group_index, group in enumerate(clause.any_of):
                missing = _unsatisfiable_workloads(
                    group, f"{clause_path}.any_of[{group_index}]", ignore_network,
                    stop_at_first=stop_at_first,
                )
                if not missing:
                    refused = []
                    break
                refused.extend(missing)
            if not refused:
                continue

            failures.append(
                f"{clause_path}: no group fits; "
                + "; ".join(refused)
                + " cannot be satisfied locally or by any known peer."
            )
            if stop_at_first:
                return failures

    return failures


def evaluate_possible_environment_workloads(
        service: celaut.Service,
        ignore_network: Optional[str] = None,
) -> Optional[str]:
    """The reason to refuse this launch, or None to let it proceed.

    Every clause of ``initial_environment_workload`` and of
    ``possible_environment_workload`` must have a group whose workloads all fit
    somewhere for the launch to be admitted -- that is the rule, and neither policy
    below changes it.

    ``workload_admission.POLICY`` decides how much is probed: ``fail_fast`` (the
    default) stops at the first clause that fits nowhere, since the launch is already
    refused and each further workload costs a gRPC round-trip to every known peer;
    ``full`` probes all of them so the message names every one.

    ``workload_admission.ON_UNSATISFIABLE`` decides what that means: ``reject`` (the
    default) returns the message, so the caller refuses the launch; ``warn`` logs it
    and returns None. The second exists because this check reads capacity *right now*
    and reserves nothing, so a busy moment can refuse a service that would have run.
    """
    failures = _unsatisfiable_clauses(
        service,
        ignore_network,
        stop_at_first=_probe_policy() == PROBE_UNTIL_FIRST_FAILURE,
    )

    if not failures:
        return None

    message = (
        "Declared descendant workload(s) the network cannot currently satisfy: "
        + " | ".join(failures)
    )

    if _on_unsatisfiable() == ON_UNSATISFIABLE_WARN:
        log.LOGGER(
            f"[WORKLOAD ADMISSION] {message} Launching anyway "
            f"(workload_admission.ON_UNSATISFIABLE={ON_UNSATISFIABLE_WARN})."
        )
        return None

    return message


def evaluate_start_funding(
        service: celaut.Service,
        metadata: celaut.Metadata,
        initial_mu: int,
        service_hash: Optional[str] = None,
        arch: Optional[str] = None,
) -> Optional[str]:
    """The reason to refuse a start whose ``initial_mu`` cannot pay the initial children.

    Only for a request that carries its own ``initial_mu``: with none, the default
    balance of the node includes ``children_initial_cost`` (``manager.default_initial_balance``).
    The balance after the build of this node must pay ``children_initial_cost`` and one
    maintenance period of the service (``start_funding.underfunded_reason``). A service
    with no initial children is not checked here.

    ``workload_admission.ON_UNDERFUNDED`` decides what a shortfall means: ``reject`` (the
    default) returns the message, so the caller refuses the launch; ``warn`` logs it and
    returns None.

    The figures are the ones of the quote (``children_initial_cost_mu`` and the same
    functions for the build and the period), at ``execution_cost.base_load``: the price
    with no load surcharge. The client funds a start from a quote that it got earlier, at
    the load of that moment, and the load moves. At the load of the start, the check
    asked for up to ``SCARCITY_MAX_MULTIPLIER`` times the base price and refused a start
    funded exactly as the quote said. At the base load, the check asks for the least that
    any quote of this node gives while it charges, so it never asks for more than the
    quote. While the node is free, the start charges nothing and is not checked.
    """
    # Imported here: start_funding pulls in the pricing modules, and this module stays
    # importable without them (see check_resource_availability_on_peer).
    from src.utils.cost_functions import start_funding
    from src.utils.cost_functions.execution_cost import base_load, is_free, system_scarcity

    if is_free(system_scarcity()):
        return None
    scarcity = base_load()
    children_mu = start_funding.children_initial_cost_mu(service, scarcity=scarcity)
    if children_mu <= 0:
        return None
    try:
        build_mu = start_funding.start_build_mu(metadata=metadata, scarcity=scarcity)
    except Exception as e:
        # A service that this node cannot build here can still run on a peer, which
        # applies its own build. The children and the period are still checked.
        log.LOGGER(f"[WORKLOAD ADMISSION] Could not price the build for the funding check ({e}); using 0.")
        build_mu = 0
    message = start_funding.underfunded_reason(
        initial_mu=initial_mu,
        build_mu=build_mu,
        minimum_mu=start_funding.minimum_runtime_mu(
            resources=service.container.resources,
            service_hash=service_hash,
            arch=arch,
            service=service,
            scarcity=scarcity,
        ),
        children_mu=children_mu,
    )
    if message is None:
        return None

    if _on_underfunded() == ON_UNDERFUNDED_WARN:
        log.LOGGER(
            f"[WORKLOAD ADMISSION] {message} Launching anyway "
            f"(workload_admission.ON_UNDERFUNDED={ON_UNDERFUNDED_WARN})."
        )
        return None

    return message
