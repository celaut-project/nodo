from typing import Optional
import traceback

from protos import celaut_pb2 as celaut, celaut_pb2
from src.balancers.execution_balancer.execution_balancer import execution_balancer, estimate_cost_on_peer
from src.gateway.launcher.delegate_execution.delegate_execution import delegate_execution
from src.gateway.launcher.local_execution.local_execution import local_execution
from src.manager.manager import default_initial_balance, descends_from_dev_client, spend_mu
from src.utils import activity_window, utils, logger as log
from src.utils.tools.recursion_guard import RecursionGuard
from src.utils.utils import from_amount, to_amount
from src.database.sql_connection import SQLConnection
from src.virtualizers.firewall import allow_connection_to_instance
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils.cost_functions.resource_availability import get_resource_availability
from src.utils.cost_functions.workload_admission import evaluate_possible_environment_workloads
from src.virtualizers.architecture import UnsupportedArchitectureException, get_arch_tag
from src.utils.network_policy import enforce_network_policy
from src.utils.shared_filesystems import service_requires_parent_colocation

sc = SQLConnection()


def _detect_local_preflight_failure(
        service: celaut.Service,
        metadata: celaut.Metadata,
        configuration: celaut_pb2.Configuration,
) -> Optional[str]:
    try:
        estimated_cost = generate_estimated_cost(
            resources=service.container.resources,
            metadata=metadata,
            config=configuration,
            arch=get_arch_tag(service=service, metadata=metadata),
        )
        if estimated_cost:
            return None

        availability = get_resource_availability(service.container.resources)
        return availability.get("reason") or "Local execution was rejected due to insufficient resources."
    except UnsupportedArchitectureException as exc:
        return str(exc)
    except Exception as exc:
        return f"Local preflight failed with {type(exc).__name__}: {exc}"


def _format_launch_failure(
        service_id: Optional[str],
        launch_failures: list[str],
        local_preflight_failure: Optional[str] = None,
) -> str:
    details = []
    if local_preflight_failure:
        details.append(f"local preflight: {local_preflight_failure}")
    details.extend(launch_failures)

    if not details:
        return (
            f"Unable to launch service {service_id}: no eligible local executor or peer "
            f"was available at this time."
        )

    return f"Unable to launch service {service_id}. Attempt details: {' | '.join(details[:8])}"


def _force_delegate(
        forced_peer: str,
        service: celaut.Service,
        service_id: str,
        metadata: celaut.Metadata,
        configuration: celaut_pb2.Configuration,
        father_id: str,
        father_ip: str,
        recursion_guard_token: str,
) -> celaut_pb2.ServiceInstance:
    """`nodo force_execution`'s bypass: delegate straight to `forced_peer`, with
    no comparison against `local` or any other peer, and no cheapest-first
    fallback if it fails. Still runs the same cost accounting a
    balancer-selected candidate would (`estimate_cost_on_peer`, `spend_mu`,
    `delegate_execution`'s own `balance_on_other_peer` check) -- only peer
    *selection* is skipped, not the economics of running the service.
    """
    if not sc.peer_exists(forced_peer):
        raise Exception(f"force_execution: peer '{forced_peer}' is not connected (see `nodo peers`).")

    if service_requires_parent_colocation(service):
        raise Exception(
            f"force_execution: service {service_id} inherits a shared filesystem from "
            f"its parent and must run on the local node; it cannot be forced onto peer "
            f"'{forced_peer}'."
        )

    log.LOGGER(f"force_execution: bypassing the balancer, delegating straight to peer {forced_peer}.")

    estimated_cost = estimate_cost_on_peer(
        peer_id=forced_peer,
        resources=service.container.resources,
        metadata=metadata,
        configuration=configuration,
        recursion_guard_token=recursion_guard_token,
    )
    if estimated_cost is None:
        raise Exception(
            f"force_execution: peer '{forced_peer}' did not return a cost estimate "
            f"for service {service_id} (unreachable, or it doesn't have the service)."
        )

    refund_container = []
    if not spend_mu(
            id=father_id,
            amount_mu=from_amount(estimated_cost.cost),
            refund_function_container=refund_container
    ):
        raise Exception(f"force_execution: error charging {father_id}.")

    instance = delegate_execution(
        service_id=service_id,
        peer=forced_peer,
        father_id=father_id,
        cost=from_amount(estimated_cost.cost),
        metadata=metadata,
        config=configuration,
        recursion_guard_token=recursion_guard_token,
        refund_container=refund_container,
        father_ip=father_ip,
    )

    # Mirrors the same post-delegation firewall step the balancer-driven loop
    # below runs for a delegated instance (see launch_service).
    if "rundev" not in father_id and sc.internal_instance_exists(id=father_id):
        try:
            if not allow_connection_to_instance(vmachine_id=father_id, instance=instance.instance):
                log.LOGGER(
                    f"Firewall allow_connection_to_instance failed for parent instance {father_id}"
                )
        except Exception as e:
            log.LOGGER(f"Exception blocking firewall rules to {father_id} for the dependency {str(instance)}")
            raise e

    return instance


def launch_service(
        service: celaut.Service,
        metadata: celaut.Metadata,
        father_ip: str,
        father_id: Optional[str] = None,
        service_id: str = None,
        configuration: Optional[celaut_pb2.Configuration] = None,
        recursion_guard_token: str = None,
) -> celaut_pb2.ServiceInstance:

    with RecursionGuard(
            token=recursion_guard_token,
            generate=bool(father_id)  # Use only if is from outside.
    ) as recursion_guard_token:

        # Check father id.
        if not father_id:
            father_id = sc.get_local_instance_id_by_uri(father_ip)
            if not father_id:
                raise Exception('Client id not provided.')
            else:
                log.LOGGER(f"Service launch request made by the service {father_id}.")
        else:
            log.LOGGER(f"Service launch request made by the client {father_id}.")

        # The operator's hours (`activity_window`). First thing after the requester is
        # known, and before anything is priced, charged or built: outside the window
        # this node takes no new work at all. Work descended from a dev client is
        # exactly what "no new work" does not mean -- `nodo execute`, the core services
        # and `nodo pack` all run under one, and the window is there to stop this
        # machine being rented out after hours, not to lock its owner out of it.
        #
        # A running instance asking for a child is new work too, and is refused with
        # everything else: a node that is closed cannot half-take a workload. That is
        # felt by a service that spawns children on demand, which is the honest cost of
        # closing a node while its instances are still running -- `ON_CLOSE: stop` is
        # the setting for an operator who would rather they were not.
        if not activity_window.is_open() and not descends_from_dev_client(father_id):
            reason = activity_window.closed_reason()
            log.LOGGER(f"Refusing to launch service {service_id}: {reason}")
            raise Exception(f"Unable to launch service {service_id}: {reason}")

        # Check configuration
        if not configuration:
            configuration = celaut_pb2.Configuration()

        if not configuration.HasField('initial_mu') or not configuration.initial_mu:
            # Funded for what it asked for, not a flat amount: a 128 MiB instance and an
            # 8 GiB one no longer start with the same balance.
            configuration.initial_mu.CopyFrom(to_amount(
                default_initial_balance(
                    system_resources=service.container.resources.at_init,
                    service_hash=service_id,
                    arch=get_arch_tag(service=service, metadata=metadata),
                )
            ))

        # The operator's network policy, before the balancer -- and before the
        # force_execution bypass below, which overrides peer *selection* and not
        # what this node is willing to have reached on its behalf. Enforced here
        # rather than in the local branch because a node that refuses to reach a
        # domain itself and then pays a peer to reach it has not applied a policy,
        # it has outsourced one. The raise carries the report the client reads:
        # being told which pattern refused which tag is the point (#280).
        enforce_network_policy(
            networks=service.network,
            subject=f"service {service_id}" if service_id else "this service",
        )

        # Refuse admission when a declared descendant workload scenario
        # (service.json's `possible_environment_workload`) has no shape that
        # fits anywhere -- neither here nor on any known peer -- right now.
        # Ahead of `forced_peer` on purpose: forcing which peer runs *this*
        # instance doesn't change whether its descendants could ever run.
        workload_admission_failure = evaluate_possible_environment_workloads(
            service=service,
            ignore_network=utils.get_network_name(direction=father_ip),
        )
        if workload_admission_failure:
            log.LOGGER(f"Refusing to launch service {service_id}: {workload_admission_failure}")
            raise Exception(
                f"Unable to launch service {service_id}: {workload_admission_failure}"
            )

        # `nodo force_execution` bypass (testing/dev only): the call carries a
        # forced-peer hint correlated via `recursion_guard_token`, never
        # `father_id` -- dev client ids are drawn from a small reusable pool
        # (manager.get_execute_client), so a hint keyed on the client id could
        # leak onto a later, unrelated `execute` call that draws the same
        # recycled id. Popped (consumed) here so it can only ever apply to this
        # one launch. No fallback to the balancer if this fails: forced means
        # forced.
        forced_peer = sc.pop_forced_execution_peer(recursion_guard_token) if recursion_guard_token else None
        if forced_peer:
            return _force_delegate(
                forced_peer=forced_peer,
                service=service,
                service_id=service_id,
                metadata=metadata,
                configuration=configuration,
                father_id=father_id,
                father_ip=father_ip,
                recursion_guard_token=recursion_guard_token,
            )

        local_preflight_failure = _detect_local_preflight_failure(
            service=service,
            metadata=metadata,
            configuration=configuration,
        )
        launch_failures = []
        local_attempted = False

        # A service that inherits any directory from its parent (a `guest=true`
        # xattr) must run on the same node as that parent, because the shared
        # filesystem is materialized locally from the parent's export. The parent
        # launching the child is the authorization; there is no cross-node attach.
        require_parent_colocation = service_requires_parent_colocation(service)
        if require_parent_colocation:
            log.LOGGER(
                f"Service {service_id} inherits a shared filesystem from its parent "
                f"{father_id}; pinning execution to the local node (no delegation)."
            )

        for peer, estimated_cost in execution_balancer(
                resources=service.container.resources,
                service_id=service_id,
                metadata=metadata,
                ignore_network=utils.get_network_name(direction=father_ip),
                configuration=configuration,
                recursion_guard_token=recursion_guard_token,
                arch=get_arch_tag(service=service, metadata=metadata),
        ):
            try:
                if require_parent_colocation and peer != 'local':
                    log.LOGGER(
                        f"Skipping peer {peer}: service must be co-located with its parent."
                    )
                    continue

                log.LOGGER(f'Service balancer select peer {peer}')

                refund_container = []

                if not spend_mu(
                        id=father_id,
                        amount_mu=from_amount(estimated_cost.cost),
                        refund_function_container=refund_container
                ):
                    raise Exception('Launch service error charging ' + father_id)

                # Delegate the service instance execution.
                if peer != 'local':
                    instance = delegate_execution(
                        service_id=service_id,
                        peer=peer,
                        father_id=father_id,
                        cost=from_amount(estimated_cost.cost),
                        metadata=metadata,
                        config=configuration,
                        recursion_guard_token=recursion_guard_token,
                        refund_container=refund_container,
                        # Needed to pick which of our addresses to advertise if the
                        # instance has to be tunnelled through this node.
                        father_ip=father_ip
                    )

                else:
                    local_attempted = True
                    instance = local_execution(
                        config=configuration,
                        resources=service.container.resources,
                        father_id=father_id, father_ip=father_ip,
                        metadata=metadata, service=service, service_id=service_id,
                        refund_container=refund_container
                    )

                #  If the service was from an internal instance, allow it to connect to it's new dependency.
                #   There is an exception for instances with the "rundev" refix. In such cases there is no container.
                if "rundev" not in father_id and sc.internal_instance_exists(id=father_id):
                    try:
                        if not allow_connection_to_instance(
                            vmachine_id=father_id,
                            instance=instance.instance,
                        ):
                            log.LOGGER(
                                f"Firewall allow_connection_to_instance failed for parent instance {father_id}"
                            )
                            # TODO This should be controlled.
                    except Exception as e:
                        log.LOGGER(f"Exception blocking firewall rules to {father_id} for the dependency {str(instance)}")
                        raise e

                return instance

            except Exception as e:
               log.LOGGER(f"Exception launching service on peer {peer}: {str(e)}")
               log.LOGGER(traceback.format_exc())
               launch_failures.append(f"{peer}: {type(e).__name__}: {e}")
               continue

        if local_attempted:
            local_preflight_failure = None

        if require_parent_colocation and not local_attempted:
            launch_failures.append(
                "co-location: service inherits a shared filesystem from its parent "
                "but the local node could not execute it, and delegation is not "
                "allowed for parent-inherited filesystems."
            )

        _err_msg = _format_launch_failure(
            service_id=service_id,
            launch_failures=launch_failures,
            local_preflight_failure=local_preflight_failure,
        )
        log.LOGGER(_err_msg)
        raise Exception(_err_msg)
