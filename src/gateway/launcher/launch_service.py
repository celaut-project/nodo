from typing import List, Optional
import traceback

from protos import celaut_pb2 as celaut, celaut_pb2
from src.balancers.execution_balancer.execution_balancer import execution_balancer, estimate_cost_on_peer
from src.gateway.launcher.delegate_execution.delegate_execution import delegate_execution
from src.gateway.launcher.local_execution.local_execution import local_execution
from src.database import ledger
from src.manager.manager import bind_child_token, default_initial_balance, descends_from_dev_client, spend_mu
from src.utils import activity_window, demand_history, utils, logger as log
from src.utils.tools.recursion_guard import RecursionGuard
from src.utils.utils import from_amount, to_amount
from src.database.sql_connection import SQLConnection
from src.virtualizers.firewall import allow_connection_to_instance
from src.utils.cost_functions.architecture_resources import executes_locally
from src.utils.cost_functions.delegated_price import own_price, peer_build_mu
from src.utils.cost_functions.start_funding import (
    StartAmountTooSmall,
    balance_after_build,
    minimum_runtime_mu,
    start_build_mu,
)
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils.cost_functions.resource_availability import get_resource_availability
from src.utils.cost_functions.workload_admission import evaluate_possible_environment_workloads
from src.virtualizers.architecture import UnsupportedArchitectureException, get_arch_tag
from src.utils.network_policy import enforce_network_policy, hidden_mode_rejection, tor_egress_unready_reason
from src.manager.shares import ShareAuthorizationError, authorize_network_shares, authorize_shares
from src.manager.share_location import LOCAL, ShareLocationConflict, locate_network_shares
from src.utils.shared_filesystems import declarations_for_service, service_requires_parent_colocation
from src.virtualizers.microvm import virtiofsd

sc = SQLConnection()

# The forced-peer hint that pins a launch to THIS node instead of a peer: the balancer's
# own name for the local candidate. A peer id is a public key in hex, so no peer can be
# called this. Set by a measurement that is only meaningful here -- the benchmark core
# service (src/core_services/benchmark.py) scores the node that runs it, and a peer that
# won the cost comparison would hand back that peer's numbers as ours.
FORCED_LOCAL = 'local'


def _detect_local_preflight_failure(
        service: celaut.Service,
        metadata: celaut.Metadata,
        configuration: celaut_pb2.Configuration,
) -> Optional[str]:
    # A node that runs nothing itself has no local failure to explain: it never tried
    # to run the service here, and "local preflight: insufficient resources" in the
    # error would send the operator looking at a machine that was never asked.
    if not executes_locally():
        return None
    try:
        estimated_cost = generate_estimated_cost(
            resources=service.container.resources,
            metadata=metadata,
            config=configuration,
            arch=get_arch_tag(service=service, metadata=metadata),
            service=service,
        )
        if estimated_cost:
            return None

        availability = get_resource_availability(
            service.container.resources, arch=get_arch_tag(service=service, metadata=metadata)
        )
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


def _mu(amount: celaut_pb2.Amount) -> int:
    """An amount as a whole number of MU. An unset one reads as zero."""
    return int(amount.n or 0)


def _own_price(service: celaut.Service, metadata: celaut.Metadata,
               configuration: celaut_pb2.Configuration) -> Optional[celaut_pb2.EstimatedCost]:
    """What this node charges for the service when a peer runs it (#577), or None if it
    cannot price it: then no peer is covered, and only the local node is a candidate."""
    try:
        return own_price(
            metadata=metadata, configuration=configuration,
            resources=service.container.resources,
            arch=get_arch_tag(service=service, metadata=metadata), service=service,
        )
    except Exception as e:
        log.LOGGER(f"Could not price the service for a peer: {e}")
        return None


def _client_charge_mu(peer: str, estimated_cost: celaut_pb2.EstimatedCost,
                      configuration: celaut_pb2.Configuration,
                      requested: bool = True,
                      own: Optional[celaut_pb2.EstimatedCost] = None) -> int:
    """What the father pays at the start, in MU: its ``initial_mu``, and nothing else (#563).

    The build is taken out of this amount. It is not added to it. So the most that the
    start can cost the father is this figure, and the father does not add the build to
    its own estimate.

    ``requested`` says whether the father set the amount. If it did, the amount is
    ``configuration.initial_mu`` as it is, and a candidate that it does not fund is
    skipped (``_funding_failure``). If it did not, the node uses the least amount that
    starts the service with the default balance: its own price, which is the build plus
    that balance (``configuration.initial_mu`` holds the balance that the quotes asked
    for). It is the same for ``local`` and for a peer (#577): ``own`` is the price of
    this node, and the quote of a peer is not part of it.
    """
    if requested:
        return _mu(configuration.initial_mu)
    return from_amount((estimated_cost if peer == 'local' else own).cost)


def _delegation_terms(estimated_cost: celaut_pb2.EstimatedCost,
                      configuration: celaut_pb2.Configuration,
                      own: celaut_pb2.EstimatedCost) -> tuple[int, int]:
    """The build of this node and the build of the peer, in local MU.

    The first comes out of what the father paid, and the second out of what this node
    sends the peer. ``configuration.initial_mu`` is the balance that the quotes asked
    for: both prices are the build plus it, so each build is what is left above it.
    """
    balance_asked = _mu(configuration.initial_mu)
    return peer_build_mu(own, balance_asked), peer_build_mu(estimated_cost, balance_asked)


def _funding_failure(
        peer: str,
        estimated_cost: celaut_pb2.EstimatedCost,
        configuration: celaut_pb2.Configuration,
        initial_mu: int,
        service: celaut.Service,
        metadata: celaut.Metadata,
        service_id: Optional[str],
        arch: Optional[str],
        own: Optional[celaut_pb2.EstimatedCost] = None,
) -> Optional[str]:
    """Why ``initial_mu`` does not start the service on this candidate, or None if it does.

    Checked before the father is charged, so a start that does not fit charges nothing.
    The build of this node must fit, and what is left must pay one maintenance period
    (``start_funding``). For a peer these are the build and the period of ``own``, the
    price of this node for a delegated instance, and what is left must also fund the
    build and a period of the peer, as it quoted them: it sets its own build and its own
    price at the start, and it applies the whole rule itself. A peer that rejects the
    start for a small amount is not at fault, and this node does not lower its
    reputation for that.
    """
    try:
        if peer == 'local':
            balance_after_build(
                initial_mu=initial_mu,
                build_mu=start_build_mu(metadata=metadata),
                minimum_mu=minimum_runtime_mu(
                    resources=service.container.resources,
                    service_hash=service_id,
                    arch=arch,
                    service=service,
                ),
            )
        else:
            if own is None:
                return "this node could not price the service, so no peer is covered"
            own_build, peer_build = _delegation_terms(estimated_cost, configuration, own)
            # The balance is what this node sends the peer, and the peer applies its
            # own rule to it.
            balance_mu = balance_after_build(
                initial_mu=initial_mu, build_mu=own_build,
                minimum_mu=_mu(own.init_maintenance_cost),
            )
            balance_after_build(
                initial_mu=balance_mu,
                build_mu=peer_build,
                # What the peer quoted for one period, at its current price. The peer
                # checks the worst case of its own bound at the start.
                minimum_mu=_mu(estimated_cost.init_maintenance_cost)
                if estimated_cost.HasField("init_maintenance_cost") else 0,
            )
    except StartAmountTooSmall as e:
        return str(e)
    return None


def _force_delegate(
        forced_peer: str,
        service: celaut.Service,
        service_id: str,
        metadata: celaut.Metadata,
        configuration: celaut_pb2.Configuration,
        father_id: str,
        father_ip: str,
        recursion_guard_token: str,
        share_nodes: Optional[List[str]] = None,
        initial_mu_requested: bool = True,
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

    if share_nodes and forced_peer not in share_nodes:
        raise Exception(
            f"force_execution: service {service_id} is a member of a network share "
            f"held on {', '.join(share_nodes)}; it cannot be forced onto peer '{forced_peer}'."
        )

    log.LOGGER(f"force_execution: bypassing the balancer, delegating straight to peer {forced_peer}.")

    estimated_cost = estimate_cost_on_peer(
        peer_id=forced_peer,
        resources=service.container.resources,
        metadata=metadata,
        configuration=configuration,
    )
    if estimated_cost is None:
        raise Exception(
            f"force_execution: peer '{forced_peer}' did not return a cost estimate "
            f"for service {service_id} (unreachable, or it doesn't have the service)."
        )

    # The father pays `initial_mu`, and the build of this node comes out of it (#563).
    # Nothing is charged for an amount that does not fit.
    own = _own_price(service, metadata, configuration)
    if own is None:
        raise Exception(f"force_execution: this node could not price service {service_id}.")
    initial_mu = _client_charge_mu(
        forced_peer, estimated_cost, configuration, requested=initial_mu_requested, own=own,
    )
    funding_failure = _funding_failure(
        peer=forced_peer, estimated_cost=estimated_cost, configuration=configuration,
        initial_mu=initial_mu, service=service, metadata=metadata, service_id=service_id,
        arch=get_arch_tag(service=service, metadata=metadata), own=own,
    )
    if funding_failure:
        raise Exception(f"force_execution: {funding_failure}")

    own_build_mu, peer_build_value = _delegation_terms(estimated_cost, configuration, own)
    refund_container = []
    start_charge = []
    if not spend_mu(
            id=father_id,
            amount_mu=initial_mu,
            refund_function_container=refund_container,
            kind=ledger.CHILD_START,
            movement=start_charge,
    ):
        raise Exception(f"force_execution: error charging {father_id}.")

    paid_configuration = celaut_pb2.Configuration()
    paid_configuration.CopyFrom(configuration)
    paid_configuration.initial_mu.CopyFrom(to_amount(initial_mu))
    instance = delegate_execution(
        service_id=service_id,
        peer=forced_peer,
        father_id=father_id,
        metadata=metadata,
        config=paid_configuration,
        recursion_guard_token=recursion_guard_token,
        refund_container=refund_container,
        father_ip=father_ip,
        own_build_mu=own_build_mu,
        peer_build_mu=peer_build_value,
        service=service,
    )
    bind_child_token(start_charge, instance.token)

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
        recursion_guard_hops: Optional[int] = None,
        start_ref: Optional[str] = None,
) -> celaut_pb2.ServiceInstance:
    """Start the service here or on a peer, for ``father_id``.

    ``start_ref`` is the StartReference of the request, in hex (#578). The
    ``child_start`` of each candidate carries it, so that the father can tell which of
    its requests each start comes from.
    """

    with RecursionGuard(
            token=recursion_guard_token,
            generate=bool(father_id),  # Use only if is from outside.
            remaining_hops=recursion_guard_hops,
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
            # Counted apart from every other refusal (issue #337): this is the only one
            # the operator can undo by moving an edge on the SCHEDULE page, so it is
            # what makes "these are my hours" answerable as "and this is what they cost
            # me". A refusal for want of memory would have happened at any hour.
            demand_history.record_refusal(because_closed=True)
            raise Exception(f"Unable to launch service {service_id}: {reason}")

        # Check configuration
        if not configuration:
            configuration = celaut_pb2.Configuration()

        # Did the father say how much it pays? An absent amount and a zero one are the
        # same here (see Configuration.initial_mu).
        initial_mu_requested = configuration.HasField('initial_mu') \
            and int(configuration.initial_mu.n or 0) > 0

        if not initial_mu_requested:
            # Funded for what it asked for, not a flat amount: a 128 MiB instance and an
            # 8 GiB one no longer start with the same balance. This is the balance that
            # the quotes ask for. What the father pays is the quote of the candidate: the
            # build of the candidate plus this balance (`_client_charge_mu`, #563).
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

        # A `guest` directory is an execution precondition, not an option: if the
        # parent that launched this service does not export the share it asks
        # for, the service cannot run -- here or anywhere. Checked before the
        # balancer and before any MU is spent, and deliberately not inside
        # `_detect_local_preflight_failure`, which reports a *local* failure worth
        # trying another candidate for. There is no other candidate: the export is
        # materialized from the parent's own rootfs, on the parent's own node.
        #
        # A network share (#268) needs no exporter, but it is held on one node: a
        # member placed anywhere else would get an empty directory under the same
        # id. So the node that holds it is found here, before the balancer, and the
        # member runs there. When no node holds it yet, placement is free.
        try:
            authorize_shares(service=service, father_id=father_id, config=configuration)
            network_shares = authorize_network_shares(
                service=service, father_id=father_id, config=configuration
            )
            share_nodes = locate_network_shares(
                network_shares, ignore_network=utils.get_network_name(direction=father_ip)
            )
        except (ShareAuthorizationError, ShareLocationConflict) as e:
            log.LOGGER(f"Refusing to launch service {service_id}: {e}")
            raise Exception(f"Unable to launch service {service_id}: {e}") from e
        if share_nodes:
            log.LOGGER(
                f"Service {service_id} is a member of a network share held on "
                f"{', '.join(share_nodes)}; restricting execution to them."
            )

        # Every share is served by a virtiofsd on this host, so check it here too,
        # before anything is charged or built (#478). A service with a `guest`
        # directory can only run here, so a missing daemon ends the launch. A service
        # that only exports can still run on a peer: this node stops being a
        # candidate, and the reason goes into the failure message if no peer takes it.
        local_virtiofsd_failure = None
        share_declarations = declarations_for_service(service)
        if share_declarations:
            try:
                virtiofsd.require_usable()
            except virtiofsd.VirtiofsdUnavailable as e:
                if any(d.guest for d in share_declarations) or share_nodes == [LOCAL]:
                    log.LOGGER(f"Refusing to launch service {service_id}: {e}")
                    raise Exception(f"Unable to launch service {service_id}: {e}") from e
                log.LOGGER(f"Service {service_id} cannot run on this node: {e}")
                local_virtiofsd_failure = str(e)

        # Hidden mode (routing.HIDDEN): this node does not run a service whose traffic
        # would leave the host directly, because that traffic shows its IP address. It is
        # a reason not to run it *here*, not a refusal of the domain: a peer that is not
        # hidden can run it, and this node calls that peer through Tor. So the local
        # candidate is skipped, as for a missing virtiofsd, and the balancer goes on.
        local_hidden_failure = None
        hidden_rejection = hidden_mode_rejection(
            networks=service.network,
            subject=f"service {service_id}" if service_id else "this service",
        )
        if hidden_rejection is not None:
            local_hidden_failure = (
                f"hidden mode does not run '{hidden_rejection.tag}' on this node: "
                f"{hidden_rejection.reason}"
            )
            log.LOGGER(f"Service {service_id} cannot run on this node: {local_hidden_failure}")
        else:
            # The services' egress goes through Tor, and Tor is not up yet: a guest
            # started now would get no egress at all, so a peer runs it instead.
            local_hidden_failure = tor_egress_unready_reason(service.network)
            if local_hidden_failure:
                log.LOGGER(f"Service {service_id} cannot run on this node now: {local_hidden_failure}")

        # `nodo force_execution` bypass (testing/dev only): the call carries a
        # forced-peer hint correlated via `recursion_guard_token`, never
        # `father_id` -- dev client ids are drawn from a small reusable pool
        # (manager.get_execute_client), so a hint keyed on the client id could
        # leak onto a later, unrelated `execute` call that draws the same
        # recycled id. Popped (consumed) here so it can only ever apply to this
        # one launch. No fallback to the balancer if this fails: forced means
        # forced.
        forced_peer = sc.pop_forced_execution_peer(recursion_guard_token) if recursion_guard_token else None
        pin_local = forced_peer == FORCED_LOCAL
        if pin_local and share_nodes and LOCAL not in share_nodes:
            raise Exception(
                f"Unable to launch service {service_id}: it is pinned to the local node, "
                f"but it is a member of a network share held on {', '.join(share_nodes)}."
            )
        if pin_local:
            log.LOGGER(f"Service {service_id} is pinned to the local node (no delegation).")
        elif forced_peer:
            return _force_delegate(
                forced_peer=forced_peer,
                service=service,
                service_id=service_id,
                metadata=metadata,
                configuration=configuration,
                father_id=father_id,
                father_ip=father_ip,
                recursion_guard_token=recursion_guard_token,
                share_nodes=share_nodes,
                initial_mu_requested=initial_mu_requested,
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
            # Co-location wins over `network.EXECUTE_LOCALLY: false`, and is not a
            # loophole in it. The shared filesystem is materialized locally from the
            # parent's own export: there is no cross-node attach, so "run it on a peer"
            # is not a thing this service can do, and the balancer will offer no local
            # candidate for it to be run as. The launch fails below with the
            # co-location message rather than silently running here -- which is the
            # honest outcome, since what was asked for is impossible rather than
            # merely disallowed.
            log.LOGGER(
                f"Service {service_id} inherits a shared filesystem from its parent "
                f"{father_id}; pinning execution to the local node (no delegation)."
            )

        # One price of this node for the whole launch (#577): the balancer keeps the peers
        # that it covers, and a father whose service a peer runs pays it.
        own = None if pin_local or require_parent_colocation \
            else _own_price(service, metadata, configuration)
        for peer, estimated_cost in execution_balancer(
                resources=service.container.resources,
                service_id=service_id,
                metadata=metadata,
                ignore_network=utils.get_network_name(direction=father_ip),
                configuration=configuration,
                recursion_guard_token=recursion_guard_token,
                arch=get_arch_tag(service=service, metadata=metadata),
                service=service,
                own=own,
        ):
            try:
                if require_parent_colocation and peer != 'local':
                    log.LOGGER(
                        f"Skipping peer {peer}: service must be co-located with its parent."
                    )
                    continue
                if pin_local and peer != 'local':
                    log.LOGGER(f"Skipping peer {peer}: this launch is pinned to the local node.")
                    continue
                if share_nodes and peer not in share_nodes:
                    log.LOGGER(
                        f"Skipping peer {peer}: the network share is held on {', '.join(share_nodes)}."
                    )
                    continue
                if peer == 'local' and (local_virtiofsd_failure or local_hidden_failure):
                    # Every reason, not the first: a node without virtiofsd that is
                    # also hidden has two, and the operator must fix both.
                    for failure in (local_virtiofsd_failure, local_hidden_failure):
                        if failure:
                            launch_failures.append(f"local: {failure}")
                    continue

                # What the father pays: its `initial_mu`, and only that (#563). The build
                # is taken out of it. The father pays what this node advertises, whoever
                # runs the service (#577).
                initial_mu = _client_charge_mu(
                    peer, estimated_cost, configuration, requested=initial_mu_requested,
                    own=own,
                )

                # An amount that does not fund the build and one maintenance period
                # charges nothing: the next candidate may need less.
                funding_failure = _funding_failure(
                    peer=peer,
                    estimated_cost=estimated_cost,
                    configuration=configuration,
                    initial_mu=initial_mu,
                    service=service,
                    metadata=metadata,
                    service_id=service_id,
                    arch=get_arch_tag(service=service, metadata=metadata),
                    own=own,
                )
                if funding_failure:
                    launch_failures.append(f"{peer}: {funding_failure}")
                    continue

                log.LOGGER(f'Service balancer select peer {peer}')

                refund_container = []
                # The `child_start` of the father. The child does not exist yet, so
                # its token is set on the row once it does (`bind_child_token`).
                start_charge = []

                if not spend_mu(
                        id=father_id,
                        amount_mu=initial_mu,
                        refund_function_container=refund_container,
                        kind=ledger.CHILD_START,
                        detail={"start_ref": start_ref} if start_ref else None,
                        movement=start_charge,
                ):
                    raise Exception('Launch service error charging ' + father_id)

                # The configuration of this candidate carries what the father paid. The
                # configuration of the launch keeps the balance that the quotes ask for,
                # which the next candidate needs.
                paid_configuration = celaut_pb2.Configuration()
                paid_configuration.CopyFrom(configuration)
                paid_configuration.initial_mu.CopyFrom(to_amount(initial_mu))

                # Delegate the service instance execution.
                if peer != 'local':
                    own_build_mu, peer_build_value = _delegation_terms(
                        estimated_cost, configuration, own)
                    instance = delegate_execution(
                        service_id=service_id,
                        peer=peer,
                        father_id=father_id,
                        metadata=metadata,
                        config=paid_configuration,
                        recursion_guard_token=recursion_guard_token,
                        refund_container=refund_container,
                        # Needed to pick which of our addresses to advertise if the
                        # instance has to be tunnelled through this node.
                        father_ip=father_ip,
                        own_build_mu=own_build_mu,
                        peer_build_mu=peer_build_value,
                        service=service,
                    )

                else:
                    local_attempted = True
                    instance = local_execution(
                        config=paid_configuration,
                        resources=service.container.resources,
                        father_id=father_id, father_ip=father_ip,
                        metadata=metadata, service=service, service_id=service_id,
                        refund_container=refund_container
                    )
                bind_child_token(start_charge, instance.token)

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

                # Work this node took (issue #337). Here rather than earlier because
                # this is the first point at which the instance is actually running and
                # reachable -- counting an admission before that would count attempts.
                demand_history.record_admission()
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

        if share_nodes and not any(
            f.startswith(f"{node}:") for f in launch_failures for node in share_nodes
        ) and not (LOCAL in share_nodes and local_attempted):
            launch_failures.append(
                f"network share: the share is held on {', '.join(share_nodes)}, and the "
                f"balancer did not offer any of those nodes as a candidate."
            )

        _err_msg = _format_launch_failure(
            service_id=service_id,
            launch_failures=launch_failures,
            local_preflight_failure=local_preflight_failure,
        )
        log.LOGGER(_err_msg)
        # Demand this node could not meet: every place it could have run was tried and
        # none would. Counted, while the validation failures above are not -- a
        # malformed request is not work anybody could have taken, and folding the two
        # together would make the history read as a capacity problem whenever a client
        # sent a bad message.
        demand_history.record_refusal()
        raise Exception(_err_msg)
