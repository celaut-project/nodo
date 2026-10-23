import time
from hashlib import sha256
from typing import Callable, List, Optional, Tuple

from src.utils.bee_client import BeeClient
from src.utils.config import ConfigManager

from protos import celaut_pb2
from src.database import ledger
from src.manager import balance_statement, statement_check
from src.manager.manager import get_client_id_on_other_peer
from src.utils.utils import from_amount
from src.manager.metrics import balance_on_other_peer, instance_balance_on_peer, refresh_balance_on_peer
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


def _stop_on_peer(peer: str, external_token: str) -> None:
    """Stop a child that ``peer`` started and that this node will not keep.

    The father got his MU back and an error, so nobody holds this instance. Left alone it
    would run on the MU that this node sent until they ran out. The peer writes what it
    leaves in our account as a ``refund`` of the ``child_start``. Never raises: the
    start has failed already, and this is only the cleanup of it.
    """
    try:
        BeeClient.stop_service(peer_channel(peer), token=external_token)
    except Exception as e:
        log.LOGGER(f"Could not stop the child {external_token[:8]} that {peer} started: {e}")


def _child_balance_on_peer(peer: str, external_token: str) -> Optional[int]:
    """The balance that ``peer`` reports for the child after the start, in its own MU."""
    try:
        return int(instance_balance_on_peer(peer_id=peer, token=external_token))
    except Exception as e:
        log.LOGGER(f'Cannot read the balance of the child {external_token[:8]} on {peer}: {e}')
        return None


def _check_start(peer: str, client_id: str, external_token: str,
                 started_at: Optional[float] = None) -> Tuple[Optional[int], Optional[dict]]:
    """Read the statement of the new child on ``peer`` and check its chain (#570).

    ``started_at`` is when this node asked the peer to start the child: the account was
    written after that, and a build may take minutes before ``StartService`` answers.

    Returns the balance of the child there, in the MU of the peer, and the mark that the
    maintenance tick goes on from: ``seq``, ``hash`` and ``ts``. The mark is None when
    the statement could not be used, and the balance then comes from ``GetMetrics``.

    A statement that does not start from the genesis of the account, or that does not
    come from a peer that just answered ``StartService``, costs the peer the penalty for
    a statement that does not add up. A peer that answers ``UNIMPLEMENTED`` (it
    delegated the child on) or that refuses for lack of balance that our balance, read
    again, does not pay for, costs it nothing. What the start took is not judged: the child holds
    what is left of what was sent, and whoever runs the child is who pays the build. The
    start goes on in both cases: the child runs.
    """
    # The read asks for no more than our balance on the peer pays for: what the start left
    # may be little, and a refusal for lack of balance is then not the peer's fault. The
    # balance is never less than the one that the peer signed, less what this node caused
    # since (``statement_check.signed_floor``).
    try:
        rows_asked = statement_check.page_rows(
            max(refresh_balance_on_peer(peer), statement_check.floor_on(peer, SQLConnection())),
            statement_check.stored_rates(peer))
    except Exception as e:
        log.LOGGER(f'Cannot read our balance on {peer} to read the statement of the child: {e}')
        rows_asked = 0
    if rows_asked <= 0:
        return _child_balance_on_peer(peer, external_token), None
    try:
        statement = statement_check.read(
            peer, client_id, token=external_token, from_seq=1, rows=rows_asked)
    except Exception as e:
        statement_check.penalise_no_statement(
            peer, external_token, e, seq=1, child=True,
            pays=lambda: statement_check.still_pays(
                peer, rows_asked, statement_check.floor_on(peer, SQLConnection())))
        return _child_balance_on_peer(peer, external_token), None

    # A new account: its movement 1 hangs from its genesis.
    now = time.time()
    start = statement_check.Mark.new_account(
        external_token, ts=now if started_at is None else started_at)
    chain = statement_check.verify_chain(statement, start, now=now, from_seq=1)
    if chain.rows is None:
        statement_check.penalise_chain(peer, external_token, chain, 1)
        return _child_balance_on_peer(peer, external_token), None

    new = statement_check.mark_after(chain, statement, start)
    mark = {"seq": new.seq, "hash": new.hash, "ts": new.ts} if chain.rows else None
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
                   ) -> celaut_pb2.ServiceInstance:
    """Run this service on ``peer`` and open the child's local account.

    ``config.initial_mu`` is what the father paid, in local MU (#563). This node keeps
    ``commission_mu`` of it and sends the peer the rest, in the MU of the peer. The peer
    takes its own build from that amount, and what is left is the balance of the child
    there. ``build_mu`` is the build that the peer quoted, in local MU.

    A delegated child keeps its deposit on this node, exactly as a local one does, so
    the father is refunded from this node's own books when the child stops. That deposit
    is what the father can get back: the amount that was sent, less the build of the
    peer, in local MU. Its account here opens with the ``start``, the ``commission`` of
    this node and that ``build``.

    The balance of the child on the peer comes from its statement (#570), which also
    shows what the peer took for the start: ``_check_start`` checks it, and the mark it
    returns is where the maintenance tick goes on from.
    """
    # The token of the child once the peer has started it. A start that times out has none
    # to stop: TODO(#578). Its attempt stays unbound, and the peer may still take its start
    # from our account (``statement_check.account_faults``).
    external_token: Optional[str] = None
    attempt_id: Optional[int] = None
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
        # Written down before the peer is asked: the peer takes MU from our account for
        # this start, and the movement may show before this node knows the child, or when
        # it never does (a timeout). See `statement_check.account_faults`.
        started_at = time.time()
        attempt_id = SQLConnection().add_start_attempt(peer, sent_peer_mu)
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
        if service_instance is None:
            raise RuntimeError(f'{peer} closed the start with no answer.')
        external_token = service_instance.token
        SQLConnection().bind_start_attempt(attempt_id, external_token)
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
        child_balance_peer, mark = _check_start(peer, client_id, external_token, started_at)
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
        if external_token:
            _stop_on_peer(peer, external_token)
        elif attempt_id is not None and not statement_check.unreachable(e):
            # The peer refused the start. It gives back the start of each candidate that
            # it tried before it answers, so the attempt holds no new movement.
            try:
                SQLConnection().refuse_start_attempt(attempt_id)
            except Exception as refuse_error:
                log.LOGGER(f'Could not close the start attempt on {peer}: {refuse_error}')
        try:
            refund_container.pop()()
        except IndexError:
            pass
        raise e
