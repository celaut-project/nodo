import time
from hashlib import sha256
from typing import Callable, List, Optional, Tuple

from src.utils.bee_client import BeeClient
from src.utils.config import ConfigManager

from protos import celaut_pb2
from src.database import ledger
from src.manager import balance_statement, statement_check
from src.manager.manager import get_client_id_on_other_peer
from src.reputation_system.reasons import Reason
from src.utils.utils import from_amount
from src.manager.metrics import balance_on_other_peer, instance_balance_on_peer
from src.database.sql_connection import SQLConnection
from src.tunneling import delegated_endpoints
from src.utils import utils, logger as log
from src.identity.grpc_transport import peer_channel
from src.utils.monetary import format_mu
from src.payment_system.mu_conversion import (
    configuration_for_peer,
    convert_mu,
    matching_payment_system,
    peer_mu_in_local,
)
from src.utils.cost_functions.start_funding import StartAmountTooSmall


env_manager = ConfigManager()
START_SERVICE_ON_PEER_TIMEOUT = int(env_manager.get("START_SERVICE_ON_PEER_TIMEOUT"))


def _publish_locally_if_needed(
        external_token: str,
        peer: str,
        instance: celaut_pb2.Instance,
        father_id: str,
        father_ip: str,
) -> celaut_pb2.Instance:
    """Return the instance to hand to our client, tunnelled through us if needed."""
    if not delegated_endpoints.should_tunnel(instance):
        return instance

    advertise_ip = delegated_endpoints.advertise_ip_for(father_id=father_id, father_ip=father_ip)
    if not advertise_ip:
        log.LOGGER(
            'Delegated instance needs a local tunnel endpoint but no address of ours '
            f'is usable for {father_id or father_ip}; handing over the peer addresses.'
        )
        return instance

    try:
        peer_gateway = next(utils.generate_uris_by_peer_id(peer))
    except StopIteration:
        log.LOGGER(f'No reachable gateway address for peer {peer}; cannot tunnel.')
        return instance

    return delegated_endpoints.publish(
        token=external_token,
        peer_gateway=peer_gateway,
        instance=instance,
        bind_ip=advertise_ip,
        peer_id=peer,
    )


def _mu(amount) -> int:
    """A configuration or quote field as a whole number of MU. Unset fields read as zero.

    `Amount.n` is a string, and a message that leaves the field out carries the empty
    one rather than a zero -- which `from_amount` cannot parse. The same reading
    `estimated_cost_for_local` takes of the same field.
    """
    return int(amount.n or 0)


def _child_balance_on_peer(peer: str, external_token: str) -> Optional[int]:
    """The balance that ``peer`` reports for the child after the start, in its own MU."""
    try:
        return int(instance_balance_on_peer(peer_id=peer, token=external_token))
    except Exception as e:
        log.LOGGER(f'Cannot read the balance of the child {external_token[:8]} on {peer}: {e}')
        return None


def _check_start(peer: str, client_id: str, external_token: str, sent_peer_mu: int,
                 start_bound_peer_mu: int) -> Tuple[Optional[int], Optional[dict]]:
    """Read the statement of the new child on ``peer`` and check its start (#570).

    Returns the balance of the child there, in the MU of the peer, and the mark that the
    maintenance tick goes on from: ``seq``, ``ts`` and ``read_at``. The mark is None when
    the statement could not be used, and the balance then comes from ``GetMetrics``.

    A statement that does not add up, or that does not come from a peer that just
    answered ``StartService``, costs the peer the penalty for a statement that does not
    add up. One that takes more than this node sent, or a build above the quote, costs
    it the penalty for an overcharge. The start goes on in both cases: the child runs.
    """
    read_at = time.time()
    try:
        statement = balance_statement.fetch(peer, client_id, token=external_token)
    except Exception as e:
        statement_check.penalise_no_statement(peer, external_token, e)
        return _child_balance_on_peer(peer, external_token), None

    # A new account: its movement 1 starts from 0.
    start = statement_check.Mark(0, 0)
    rows, problem = statement_check.new_rows(statement, start)
    if rows is None:
        if problem:
            statement_check.penalise_once(peer, external_token, Reason.STATEMENT_INCONSISTENT, problem)
        return _child_balance_on_peer(peer, external_token), None

    problems = statement_check.child_problems(
        rows, mark=start,
        rates=statement_check.stored_rates(peer),
        sent_peer_mu=sent_peer_mu, start_bound_peer_mu=start_bound_peer_mu,
    )
    if problems:
        statement_check.penalise_once(
            peer, external_token, Reason.STATEMENT_OVERCHARGED, "; ".join(problems)
        )
    mark = {"seq": rows[-1].seq, "ts": rows[-1].ts, "read_at": read_at} if rows else None
    return from_amount(statement.balance), mark


def delegate_execution(
                        service_id: str,
                        peer: str,
                        father_id: str,
                        metadata, config,
                        recursion_guard_token,
                        refund_container: List[Callable],
                        father_ip: str = "",
                        commission_mu: int = 0,
                        build_mu: int = 0,
                        variance: float = 0.0,
                   ) -> celaut_pb2.ServiceInstance:
    """Run this service on ``peer`` and open the child's local account.

    ``config.initial_mu`` is what the father paid, in local MU (#563). This node keeps
    ``commission_mu`` of it and sends the peer the rest, in the MU of the peer. The peer
    takes its own build from that amount, and what is left is the balance of the child
    there. ``build_mu`` is the build that the peer quoted, in local MU, and ``variance``
    the variance of that quote: together they bound the build that the peer may take.

    A delegated child keeps its deposit on this node, exactly as a local one does, so
    the father is refunded from this node's own books when the child stops. That deposit
    is what the father can get back: the amount that was sent, less the build of the
    peer, in local MU. Its account here opens with the ``start``, the ``commission`` of
    this node and that ``build``.

    The balance of the child on the peer comes from its statement (#570), which also
    shows what the peer took for the start: ``_check_start`` checks it, and the mark it
    returns is where the maintenance tick goes on from.
    """
    try:
        log.LOGGER('The service is launched on node ' + str(peer))

        initial_mu = _mu(config.initial_mu) if config.HasField("initial_mu") else 0
        sent_mu = initial_mu - int(commission_mu)
        if sent_mu <= 0:
            # Nothing is left for the peer after the commission: the same rejection as
            # an amount that does not fit the build, and nothing was charged to the peer.
            raise StartAmountTooSmall(
                f'initial_mu {initial_mu} MU does not cover the commission of this node, '
                f'{int(commission_mu)} MU, and a start on {peer}.'
            )

        # The configuration travels to the peer's Gateway, which reads MU on its
        # own scale, so it is translated here -- with the amount that the peer gets,
        # not the amount that the father paid. The balance check is not: `sent_mu` and
        # `balance_on_other_peer` are both already in our MU (the balance is
        # converted where it enters the node, in `manager.metrics`), and
        # converting one side of a comparison whose other side is local was how
        # this check came to compare two different scales.
        payment_system = matching_payment_system(peer)
        peer_config = configuration_for_peer(
            config, payment_system=payment_system, initial_mu=sent_mu
        )
        sent_peer_mu = _mu(peer_config.initial_mu) if peer_config.HasField("initial_mu") else 0

        if balance_on_other_peer(peer_id=peer) <= sent_mu:
            raise Exception(
                'Launch service error: not enough balance on ' + peer + '. '
                'Current: ' + format_mu(balance_on_other_peer(peer_id=peer)) + ', required: ' + format_mu(sent_mu) + '.'
            )

        log.LOGGER('Go to launch the service on ' + str(peer))
        client_id = get_client_id_on_other_peer(peer_id=peer)
        service_instance = BeeClient.start_service(
            peer_channel(peer),
            utils.service_extended(
                metadata=metadata,
                config=peer_config,
                # TODO: Could pass only the previously selected configuration with the estimate cost
                #  request, now is allowing to select another (that could be reasonable).
                client_id=client_id,
                recursion_guard_token=recursion_guard_token
            ),
            timeout=START_SERVICE_ON_PEER_TIMEOUT if START_SERVICE_ON_PEER_TIMEOUT > 0 else None,
        )
        external_token: str = service_instance.token
        encrypted_external_token: str = sha256(external_token.encode('utf-8')).hexdigest()

        build_peer_mu = convert_mu(
            int(build_mu),
            from_mu_per_unit=payment_system.local_mu_per_unit,
            to_mu_per_unit=payment_system.peer_mu_per_unit,
            round_up=True,
        )
        # What the peer says the child holds now: the amount that we sent, less the build
        # that it took. Read, not computed: the peer sets its own build, and the tick
        # below measures the consumption from this very figure.
        child_balance_peer, mark = _check_start(
            peer, client_id, external_token, sent_peer_mu,
            statement_check.build_bound(build_peer_mu, variance),
        )
        if child_balance_peer is None:
            # The peer could not be read: use what its quote says. The tick corrects the
            # mark at the next reading, and the instance is not lost over a failed read.
            peer_balance_mu = max(sent_peer_mu - build_peer_mu, 0)
            balance_mu = max(sent_mu - int(build_mu), 0)
        else:
            peer_balance_mu = child_balance_peer
            balance_in_local = peer_mu_in_local(peer, child_balance_peer)
            # Never more than what was sent: the child cannot hold more than it got.
            balance_mu = max(sent_mu - int(build_mu), 0) if balance_in_local is None \
                else min(balance_in_local, sent_mu)

        # The peer advertised its own addresses. If our client cannot reach them,
        # stand in for the service locally and hand over our endpoints instead —
        # the client speaks the service's protocol, not beeRPC, so only this node
        # can front the tunnel for it. Policy: network.DELEGATION_TUNNEL_POLICY.
        # TODO adapt for ipv6 too.
        published_instance = _publish_locally_if_needed(
            external_token=external_token,
            peer=peer,
            instance=service_instance.instance,
            father_id=father_id,
            father_ip=father_ip,
        )

        SQLConnection().add_delegated_instance(
            father_id=father_id,
            peer_id=peer,  # Add node_uri.
            encrypted_external_token=encrypted_external_token,  # Add token.
            external_token=external_token,
            # Store what the client is told, so tunnels can be restored after a
            # restart and firewall cleanup targets the address actually handed out.
            serialized_instance=published_instance.SerializeToString(),
            service_id=service_id,
            # What the father can get back, in our MU: what he paid, less the commission
            # of this node and less the build of the peer. The commission and the build
            # are the part of the start that this node and the peer keep. The
            # maintenance tick spends this figure down by what the peer meters.
            balance_mu=balance_mu,
            # The balance of the child as the peer reports it, on its scale. This is the
            # mark the first tick measures against, so it is what the peer holds after
            # it took its build: anything the child spends before the first tick is
            # still the client's to pay, and the build is not, because the build was
            # taken from the amount before the child had a balance.
            peer_balance_mu=peer_balance_mu,
            movements=(
                ledger.Movement(ledger.START, initial_mu),
                ledger.Movement(ledger.COMMISSION, -int(commission_mu)),
                ledger.Movement(ledger.BUILD, balance_mu - sent_mu,
                                detail={"peer_id": peer}),
            ),
            sent_peer_mu=sent_peer_mu,
            mark=mark,
        )
        service_instance.token = encrypted_external_token
        service_instance.instance.CopyFrom(published_instance)
        return service_instance
    except Exception as e:
        log.LOGGER('Failed starting a service on peer, occurs the error: ' + str(e))
        try:
            refund_container.pop()()
        except IndexError:
            pass
        raise e
