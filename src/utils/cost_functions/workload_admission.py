"""Admission check for ``Service.possible_environment_workload``.

`service.json` declares the worst-case descendant workloads a service may spawn
during its lifetime (issue #163); the packer serializes that declaration
without interpreting it. This module is where it is interpreted, at launch.

For each declared scenario, every workload group inside it must be satisfiable
*somewhere* -- locally, or by at least one known peer -- or the service is
refused admission. This generalizes the exact same boolean admission gate
`resource_availability.get_resource_availability` already runs for a
service's own `at_most` resources, applied instead to each descendant group's
`resources`.

Every limit a group declares is checked, not only `mem_limit`: memory and disk
against what is free right now, a CPU quota against the number of cores the
host has at all (a quota is a share of time, so refusing on instantaneous load
would make admission flap), and `blkio_weight` against the range cgroups
accept. See `resource_availability._sysreq_shortfalls`.

Two policies govern the rest, both in `config.yaml` under `workload_admission`
and both read per call:

* `POLICY` -- how much to probe. `fail_fast` (default) stops at the first group
  that fits nowhere; `full` probes every group so the message names all of them.
  This never changes *what* is admitted: every group must fit either way.
* `ON_UNSATISFIABLE` -- what a group that fits nowhere means. `reject` (default)
  refuses the launch; `warn` logs and lets it through, for an operator who would
  rather risk it than lose a launch to a momentary reading.

What this does NOT do, and should not be read as doing: it does not reserve
capacity, and it does not prove that `count` concurrent instances of a group
could all run at once (locally, or spread across peers) -- there is no
capacity ledger anywhere in this codebase that tracks that, for real services
or for these hypothetical ones. It only proves existence: that a shape this
big fits *somewhere* right now, exactly as confident as the single-service
admission check it reuses and no more. Scenarios are independent and
non-cumulative (see the proto comment on PossibleEnvironmentWorkload), so
this is a per-scenario, per-workload-group check, not a sum across the whole
service.
"""
from typing import Final, List, Optional, Tuple

from protos import celaut_pb2 as celaut
from src.utils import logger as log
from src.utils.cost_functions.resource_availability import get_resource_availability
from src.utils.config import ConfigManager

env_manager = ConfigManager()

EXTERNAL_COST_TIMEOUT = env_manager.get("EXTERNAL_COST_TIMEOUT")

# How much of the declaration to probe before answering. This is *only* about how much
# is probed, never about what is admitted: every declared group must fit, under both
# policies. What differs is whether the node keeps asking once the answer is settled.
#
#   fail_fast -- stop at the first group that fits nowhere. The service is refused the
#                moment one group fails, so every probe after that one buys nothing but
#                a longer error message, and each of them is a gRPC round-trip to every
#                known peer. The default: the rejection path must not be the expensive
#                one.
#   full      -- probe every group and report all of them. Worth its cost when a
#                declaration is being debugged and "which of my groups do not fit?" is
#                the actual question, not "may this launch proceed?".
PROBE_ALL_GROUPS: Final[str] = "full"
PROBE_UNTIL_FIRST_FAILURE: Final[str] = "fail_fast"
_DEFAULT_PROBE_POLICY: Final[str] = PROBE_UNTIL_FIRST_FAILURE

# What a group that fits nowhere means for the launch.
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


def _policy(key: str, valid: Tuple[str, ...], default: str) -> str:
    """One of ``valid``, read per call so a change needs no daemon restart.

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


def _timeout() -> Optional[int]:
    try:
        timeout = int(EXTERNAL_COST_TIMEOUT)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def check_resource_availability_on_peer(
        peer_id: str,
        resources: celaut.Service.Container.Resources,
) -> Optional[bool]:
    """Ask exactly one peer whether it could run an instance shaped like
    `resources` right now. None means the peer could not be asked (unreachable,
    timed out, or running a version without the RPC) -- not a "no".
    """
    # Imported lazily: this is the only place in the module that talks to a
    # peer, and keeping the rest importable without bee_rpc/grpc installed is
    # what lets evaluate_possible_environment_workloads' own logic be unit
    # tested on a host that has neither (see tests/test_workload_admission.py).
    import grpc
    from bee_rpc import client as bee
    from protos import celaut_pb2_grpc
    from src.utils.utils import generate_uris_by_peer_id

    # TODO(#257): a plaintext channel, like every other peer call on this path. When
    # `grpc_transport.peer_channel(peer_id)` exists it replaces this, and it resolves
    # the address too, so `generate_uris_by_peer_id` goes with it.

    try:
        response = next(bee.client_grpc(
            method=celaut_pb2_grpc.GatewayStub(
                grpc.insecure_channel(next(generate_uris_by_peer_id(peer_id)))
            ).GetResourceAvailability,
            timeout=_timeout(),
            partitions_message_mode_parser=True,
            indices_parser=celaut.ResourceAvailability,
            input=resources,
        ))
        return response.can_execute
    except Exception as e:
        log.LOGGER(f"Could not check resource availability on peer {peer_id}: {e}")
        return None


def _local_resource_availability(resources: celaut.Service.Container.Resources) -> dict:
    """Whether this host could take one instance shaped like ``resources``.

    Its own function rather than a call inlined into
    :func:`_workload_group_is_satisfiable` so a test can replace exactly this step and
    drive the peer half of the decision on its own.
    """
    return get_resource_availability(resources)


def _workload_group_is_satisfiable(
        resources: celaut.Service.Container.Resources,
        ignore_network: Optional[str],
) -> bool:
    if _local_resource_availability(resources)["can_execute"]:
        return True

    if not env_manager.get("network.DELEGATE_EXECUTION", True):
        return False

    from src.utils.utils import peers_id_iterator  # see check_resource_availability_on_peer

    for peer_id in peers_id_iterator(ignore_network=ignore_network):
        if check_resource_availability_on_peer(peer_id, resources) is True:
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


def _unsatisfiable_groups(
        service: celaut.Service,
        ignore_network: Optional[str],
        *,
        stop_at_first: bool,
) -> List[str]:
    """Every declared workload group that fits nowhere, described one per entry.

    ``stop_at_first`` returns as soon as one is found. It changes nothing about which
    services are admitted -- every group still has to fit -- only how many peers are
    asked once the answer is already settled.
    """
    failures: List[str] = []

    for scenario_index, scenario in enumerate(service.possible_environment_workload):
        for workload_index, workload in enumerate(scenario.workloads):
            if workload.count == 0 or not workload.HasField("resources"):
                continue  # No resource requirement declared; trivially satisfiable.

            resources = celaut.Service.Container.Resources(at_most=workload.resources)
            if _workload_group_is_satisfiable(resources, ignore_network):
                continue

            failures.append(
                f"possible_environment_workload[{scenario_index}].workloads[{workload_index}] "
                f"(count={workload.count}, {_declared_limits(workload.resources)}) "
                "cannot be satisfied locally or by any known peer."
            )
            if stop_at_first:
                return failures

    return failures


def evaluate_possible_environment_workloads(
        service: celaut.Service,
        ignore_network: Optional[str] = None,
) -> Optional[str]:
    """The reason to refuse this launch, or None to let it proceed.

    Every declared group must fit somewhere for the launch to be admitted -- that is
    the rule, and neither policy below changes it.

    ``workload_admission.POLICY`` decides how much is probed: ``fail_fast`` (the
    default) stops at the first group that fits nowhere, since the launch is already
    refused and each further group costs a gRPC round-trip to every known peer;
    ``full`` probes all of them so the message names every one.

    ``workload_admission.ON_UNSATISFIABLE`` decides what that means: ``reject`` (the
    default) returns the message, so the caller refuses the launch; ``warn`` logs it
    and returns None. The second exists because this check reads capacity *right now*
    and reserves nothing, so a busy moment can refuse a service that would have run.
    """
    failures = _unsatisfiable_groups(
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
