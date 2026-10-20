import math
import threading
from hashlib import sha256
from typing import Callable, Dict, List, Optional

from src.utils.bee_client import BeeClient
from src.utils.config import ConfigManager

from protos import celaut_pb2
from src.manager.manager import get_client_id_on_other_peer
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
from src.reputation_system.reasons import Reason
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


# What a peer costs in reputation when it breaks the rule of the start (#563). The scale
# is the one of the other penalties: a payment that the peer does not acknowledge is
# -100, a service that does not hash to its id is -1000. Taking money from the account of
# this node outside the amount that it sent is theft, or the old rule of the network
# (the caller pays the build), and it repeats on every start. Taking too much from the
# amount is a smaller fault: the money was the amount that this node chose to send.
CHARGED_OUTSIDE_AMOUNT_PENALTY = -1000
OVERCHARGED_PENALTY = -100

# One start at a time on each peer. The account of this node on the peer is read before
# and after the start, and the fall is compared with the amount that was sent. Another
# start on the same peer between the two reads would be a fall that the peer did not
# cause. A start is rare, so the wait is short against the build that it often includes.
_START_LOCKS: Dict[str, threading.Lock] = {}
_START_LOCKS_GUARD = threading.Lock()


def _start_lock(peer: str) -> threading.Lock:
    with _START_LOCKS_GUARD:
        return _START_LOCKS.setdefault(peer, threading.Lock())


def _account_on_peer(peer: str) -> Optional[int]:
    """What this node holds on ``peer`` now, in the MU of the peer, or None if unknown.

    Read from the peer on purpose: the cached figure of ``balance_on_other_peer`` can be
    ten seconds old. None is not an error: the check that needs it is skipped.
    """
    try:
        return int(refresh_balance_on_peer(peer))
    except Exception as e:
        log.LOGGER(f'Cannot read our account on {peer} to check the start charge: {e}')
        return None


def _child_balance_on_peer(peer: str, external_token: str) -> Optional[int]:
    """The balance that ``peer`` reports for the child after the start, in its own MU."""
    try:
        return int(instance_balance_on_peer(peer_id=peer, token=external_token))
    except Exception as e:
        log.LOGGER(f'Cannot read the balance of the child {external_token[:8]} on {peer}: {e}')
        return None


def _penalize(peer: str, amount: int, reason: str) -> None:
    try:
        from src.reputation_system import interface
        interface.update_peer_reputation(peer_id=peer, amount=amount, reason=reason)
    except Exception as e:
        log.LOGGER(f'Cannot lower the reputation of {peer} ({reason}): {e}')


def check_start_charge(
        peer: str,
        *,
        sent_peer_mu: int,
        build_peer_mu: int,
        variance: float,
        account_before: Optional[int],
        account_after: Optional[int],
        child_balance: Optional[int],
) -> None:
    """Lower the reputation of ``peer`` if it charged the start in a way the rule does not allow.

    All figures are in the MU of the peer. The only source of the start cost is the
    amount that this node sent (``sent_peer_mu``). Two things are wrong:

    * The account of this node fell by more than ``sent_peer_mu``. The peer took money
      outside the amount. A peer that keeps the old rule does this: it charges the build
      to the account of the caller, and also takes the whole amount.
    * The child holds less than ``sent_peer_mu`` less the build that the peer quoted and
      its variance allow. The peer took more from the amount than it said.

    A peer that rejects the start because the amount is too small is not here at all:
    that is an error of the start, and nobody did wrong.
    """
    if account_before is not None and account_after is not None:
        fall = int(account_before) - int(account_after)
        if fall > sent_peer_mu:
            log.LOGGER(
                f'Peer {peer} took {fall} MU of its own from our account for a start. '
                f'We sent {sent_peer_mu} MU. It took money outside the amount that we sent.'
            )
            _penalize(peer, CHARGED_OUTSIDE_AMOUNT_PENALTY, Reason.START_CHARGED_OUTSIDE_AMOUNT)
    if child_balance is not None:
        taken = int(sent_peer_mu) - int(child_balance)
        # One MU of slack for the rounding of the conversion of the quote to the MU of the peer.
        allowed = math.ceil(int(build_peer_mu) * (1 + max(float(variance), 0.0))) + 1
        if taken > allowed:
            log.LOGGER(
                f'Peer {peer} took {taken} MU from the amount for the start. '
                f'Its quote and variance allow {allowed} MU.'
            )
            _penalize(peer, OVERCHARGED_PENALTY, Reason.START_OVERCHARGED)


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
    is the variance of its quote. They are only the measure that the peer is checked
    against afterwards (``check_start_charge``): the peer sets the real build.

    A delegated child keeps its deposit on this node, exactly as a local one does, so
    the father is refunded from this node's own books when the child stops. That deposit
    is what the father can get back: the amount that was sent, less the build of the
    peer, in local MU. The peer reports the balance of the child, and that figure is
    read, because the maintenance tick charges the client by re-reading it and taking
    the difference. What the peer holds against our client there is the node's own
    wholesale position and is not mirrored per instance.

    The peer must take nothing from our account for the start but the amount that was
    sent. If the account falls by more, the peer is treated as malicious.
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
        with _start_lock(peer):
            account_before = _account_on_peer(peer)
            service_instance = BeeClient.start_service(
                peer_channel(peer),
                utils.service_extended(
                    metadata=metadata,
                    config=peer_config,
                    # TODO: Could pass only the previously selected configuration with the estimate cost
                    #  request, now is allowing to select another (that could be reasonable).
                    client_id=get_client_id_on_other_peer(peer_id=peer),
                    recursion_guard_token=recursion_guard_token
                ),
                timeout=START_SERVICE_ON_PEER_TIMEOUT if START_SERVICE_ON_PEER_TIMEOUT > 0 else None,
            )
            account_after = _account_on_peer(peer)
        external_token: str = service_instance.token
        encrypted_external_token: str = sha256(external_token.encode('utf-8')).hexdigest()

        # What the peer says the child holds now: the amount that we sent, less the build
        # that it took. Read, not computed: the peer sets its own build, and the tick
        # below measures the consumption from this very figure.
        child_balance_peer = _child_balance_on_peer(peer, external_token)
        build_peer_mu = convert_mu(
            int(build_mu),
            from_mu_per_unit=payment_system.local_mu_per_unit,
            to_mu_per_unit=payment_system.peer_mu_per_unit,
            round_up=True,
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
            balance_mu = sent_mu - int(build_mu) if balance_in_local is None \
                else min(balance_in_local, sent_mu)

        # The peer is checked after the child is safe on a row, and a failure of the
        # check never takes the instance away.
        try:
            check_start_charge(
                peer,
                sent_peer_mu=sent_peer_mu,
                build_peer_mu=build_peer_mu,
                variance=variance,
                account_before=account_before,
                account_after=account_after,
                child_balance=child_balance_peer,
            )
        except Exception as e:
            log.LOGGER(f'Error checking the start charge of {peer}: {e}')

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
