"""Admission check for ``Service.possible_environment_workload``.

The packer lets `service.json` declare the worst-case descendant workloads a
service may spawn during its lifetime (issue #163). That issue deliberately
scoped out interpreting the declaration: the spec and the packer were done,
enforcement was left "future scheduler work". This module is that follow-up.

For each declared scenario, every workload group inside it must be satisfiable
*somewhere* -- locally, or by at least one known peer -- or the service is
refused admission. This generalizes the exact same boolean admission gate
`generate_estimated_cost.get_resource_availability` already runs for a
service's own `at_most` resources, applied instead to each descendant group's
`resources`.

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
from typing import Optional

from protos import celaut_pb2 as celaut
from src.utils import logger as log
from src.utils.config import ConfigManager

env_manager = ConfigManager()

EXTERNAL_COST_TIMEOUT = env_manager.get("EXTERNAL_COST_TIMEOUT")


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
    timed out, or an old peer that doesn't implement the RPC yet) -- not a "no".
    """
    # Imported lazily: this is the only place in the module that talks to a
    # peer, and keeping the rest importable without bee_rpc/grpc installed is
    # what lets evaluate_possible_environment_workloads' own logic be unit
    # tested on a host that has neither (see tests/test_workload_admission.py).
    import grpc
    from bee_rpc import client as bee
    from protos import celaut_pb2_grpc
    from src.utils.utils import generate_uris_by_peer_id

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
    # Lazily imported: generate_estimated_cost.py's own import chain reaches
    # into the CH virtualizer build machinery (for unrelated billing helpers),
    # which needs bee_rpc symbols this module otherwise has no reason to
    # require just to evaluate a resource shape. Wrapped in its own function
    # (rather than imported straight into _workload_group_is_satisfiable) so
    # tests can replace this one name without importing any of that chain.
    from src.utils.cost_functions.generate_estimated_cost import get_resource_availability
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


def evaluate_possible_environment_workloads(
        service: celaut.Service,
        ignore_network: Optional[str] = None,
) -> Optional[str]:
    """None when every declared scenario is satisfiable; otherwise a message
    listing every workload group that isn't, so a rejection explains itself
    fully rather than pointing at only the first failure.
    """
    failures = []

    for scenario_index, scenario in enumerate(service.possible_environment_workload):
        for workload_index, workload in enumerate(scenario.workloads):
            if workload.count == 0 or not workload.HasField("resources"):
                continue  # No resource requirement declared; trivially satisfiable.

            resources = celaut.Service.Container.Resources(at_most=workload.resources)
            if _workload_group_is_satisfiable(resources, ignore_network):
                continue

            failures.append(
                f"possible_environment_workload[{scenario_index}].workloads[{workload_index}] "
                f"(count={workload.count}, mem_limit={workload.resources.mem_limit}) "
                "cannot be satisfied locally or by any known peer."
            )

    if not failures:
        return None
    return (
        "Declared descendant workload(s) the network cannot currently satisfy: "
        + " | ".join(failures)
    )
