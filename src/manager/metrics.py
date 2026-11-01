import datetime

from protos import celaut_pb2

from src.manager.manager import get_client_id_on_other_peer
from src.database.sql_connection import SQLConnection, is_peer_available

from src.identity.grpc_transport import peer_channel
from src.utils.bee_client import BeeClient
from src.utils.utils import from_amount, get_network_name, to_amount
from src.utils.logger import LOGGER as logger
from src.utils.config import ConfigManager

env_manager = ConfigManager()

sc = SQLConnection()

MANAGER_ITERATION_TIME = env_manager.get("MANAGER_ITERATION_TIME")

def __get_metrics_client(client_id: str) -> celaut_pb2.Metrics:
    """
    Retrieve metrics for a specific client from cached data.

    This function retrieves metrics for a specific client from cached data based on the provided client ID.

    :param client_id: The ID of the client for which to retrieve metrics.
    :type client_id: str
    :return: A protobuf object containing the metrics for the specified client.
    :rtype: celaut_pb2.Metrics
    :raises KeyError: If the provided client ID does not exist in the cached data.
    """
    client_balance, _, _ = sc.get_client_balance(client_id=client_id)
    return celaut_pb2.Metrics(
        balance=to_amount(client_balance),
    )


def __get_metrics_internal(id: str) -> celaut_pb2.Metrics:
    """
    Retrieve internal metrics from DB

    This function retrieves internal metrics from DB.

    :param id: The id used to identify the source of internal metrics.
    :type token: str
    :return: A protobuf object containing the internal metrics retrieved.
    :rtype: celaut_pb2.Metrics
    :raises KeyError: If the provided token does not exist in the cached data.
    """
    return celaut_pb2.Metrics(
        balance=to_amount(sc.get_instance_balance(id=id)),
        # Its father, when it delegated it here, stops charging while it is (F1 of the
        # review of #595): nothing runs, and this node charges it nothing either. Not
        # for a hibernated one (#603): it is charged for its disk, so its father keeps
        # charging it, and no suspension grace ends it.
        suspended=sc.is_local_instance_suspended(id=id)
                  and not sc.is_local_instance_hibernated(id=id),
    )


def __get_metrics_external(peer_id: str, token: str) -> celaut_pb2.Metrics:
    """
    Retrieve external metrics using gRPC communication.

    This function retrieves metrics from an external peer using gRPC communication.

    :param peer_id: The identifier of the external peer from which to retrieve the metrics.
    :type peer_id: str
    :param token: The token used to authenticate the request and retrieve the metrics.
    :type token: str
    :return: A protobuf object containing the external metrics retrieved.
    :rtype: celaut_pb2.Metrics
    """
    # With a limit: the maintenance tick asks for it, and a peer that does not answer
    # must not stop the tick (#570).
    from src.manager.statement_check import peer_call_timeout
    return BeeClient.get_metrics(peer_channel(peer_id=peer_id), token=token,
                                 timeout=peer_call_timeout())


def _in_local_mu(peer_id: str, peer_balance_mu: int) -> int:
    """The peer's MU figure, expressed in ours.

    Rounds down. This is an asset, not a debt: understating what we hold on a peer
    only ever makes us delegate or spend less than we could, while overstating it
    hands the peer a job it will refuse for lack of balance. Zero when no common
    payment system says what the peer's MU is worth -- "nothing usable there" is the
    safe answer for every caller of `balance_on_other_peer`.
    """
    from src.payment_system.mu_conversion import peer_mu_in_local

    local = peer_mu_in_local(peer_id, peer_balance_mu)
    if local is None:
        logger(f'Cannot express the balance on {peer_id} in our own MU.')
        return 0
    return local


def instance_metrics_on_peer(peer_id: str, token: str) -> celaut_pb2.Metrics:
    """The ``Metrics`` of a delegated instance as the peer keeps them: its balance, in the
    *peer's* MU, and whether the peer has it suspended. Raises as `instance_balance_on_peer`."""
    return __get_metrics_external(peer_id=peer_id, token=token)


def instance_balance_on_peer(peer_id: str, token: str) -> int:
    """A delegated instance's balance as the peer keeps it, in the *peer's* MU.

    Deliberately unconverted: the maintenance tick charges the difference between two
    of these readings, so the subtraction has to happen on the peer's own scale --
    converting each reading first would turn a rate change between them into
    consumption that never happened.

    Raises when the peer cannot be asked, or does not know the instance.
    """
    return from_amount(__get_metrics_external(peer_id=peer_id, token=token).balance)


def balance_on_other_peer(peer_id: str) -> int:
    """Our balance held on another peer, in **our** MU.

    The peer reports it in *its* MU: MU is an internal unit and two nodes do not
    share a scale. Converting here -- at the single point where that figure enters
    this node -- is what lets every caller (`delegate_execution`'s pre-flight
    check, `maintain.peer_deposits`' refill sizing) compare it against local costs
    and local deposit floors without each one repeating the conversion, or
    forgetting to.

    The stored column keeps the peer's own unconverted figure; see
    `SQLConnection.refresh_balance_for_peer`.

    :param peer_id: The identifier of the peer from which to retrieve the balance.
    :type peer_id: str
    :return: The balance in our MU. Zero if the peer is unreachable, or if no
        common payment system says what its MU is worth -- both mean "nothing
        usable there", which is the safe answer for every caller.
    :rtype: int
    """

    peer = sc.get_peer_by_id(peer_id=peer_id)
    if peer and 'balance_last_update' in peer and peer['balance_last_update']:
        last_update_time = datetime.datetime.fromisoformat(peer['balance_last_update'])
        if (datetime.datetime.now() - last_update_time).total_seconds() <= min(10.0, float(MANAGER_ITERATION_TIME)):
            return _in_local_mu(peer_id, peer['balance_mu'])

    return _in_local_mu(peer_id, _fetch_balance_on_peer(peer_id))


def refresh_balance_on_peer(peer_id: str) -> int:
    """Ask the peer what we hold there *now*, ignoring the few-seconds cache.

    ``balance_on_other_peer`` answers from the stored row when it is fresh enough, which
    is right for the hot paths and wrong for an operator who asked to see the current
    figure. Returns the peer's own MU, as stored; raises when the peer cannot be asked.
    """
    return _fetch_balance_on_peer(peer_id, raise_on_error=True)


# What ``require_caller`` raises for a client_id that the peer never minted. ``ClientRequired``
# reaches us as a generic gRPC error with no status code of its own, so the message is
# the only thing that tells it from the quarantine, which is the same exception.
UNKNOWN_CLIENT_MARKER = 'requires a client_id minted by GenerateClient'


def _is_unknown_client_answer(error: Exception) -> bool:
    import grpc
    from src.manager.statement_check import _code_of
    if _code_of(error) != grpc.StatusCode.UNKNOWN:
        return False
    return UNKNOWN_CLIENT_MARKER in (error.details() or '')


def _fathers_live_delegated(peer_id: str) -> bool:
    """Our client on the peer is the father of the children delegated there."""
    return any(row['peer_id'] == peer_id for row in sc.get_delegated_instances())


def _fetch_balance_on_peer(peer_id: str, raise_on_error: bool = False) -> int:
    """Read our balance from the peer's ``GetClientMetrics`` and store it, in the peer's MU."""
    client_id = get_client_id_on_other_peer(peer_id=peer_id)
    if not client_id:
        logger(f'No client_id for peer {peer_id}; cannot get balance.')
        if raise_on_error:
            raise ConnectionError(f'No client_id for peer {peer_id}.')
        return 0

    try:
        # With a limit, as for the instances: this is read on the maintenance tick.
        from src.manager.statement_check import peer_call_timeout
        metrics = BeeClient.get_client_metrics(
                        peer_channel(peer_id=peer_id), client_id=client_id,
                        timeout=peer_call_timeout(),
                    )
    except Exception as e:
        logger('Error getting our balance from ' + peer_id + '.')
        # Only the peer saying it does not know this client forgets it (#602). A timeout
        # (#570), an UNIMPLEMENTED from a peer that predates GetClientMetrics, the peer's
        # rate-limit quarantine or a server fault say nothing about the client, and the id
        # is the only credential for the deposit and the children delegated there.
        if _is_unknown_client_answer(e) and is_peer_available(peer_id=peer_id) \
                and not _fathers_live_delegated(peer_id):
            logger('The client is unknown on peer ' + peer_id)
            sc.delete_external_client(peer_id=peer_id)
        if raise_on_error:
            raise
        return 0

    balance = from_amount(metrics.balance)

    # Stored verbatim, in the peer's MU: it is the peer's own accounting, and
    # a cached row converted at write time would go stale the moment either
    # node changed its rate.
    sc.refresh_balance_for_peer(peer_id=peer_id, balance_mu=balance)
    return balance



def get_client_metrics(client_id: str) -> celaut_pb2.Metrics:
    """The metrics of a client: its balance. ``GetClientMetrics`` calls this after
    ``require_caller`` has identified the client; an instance token is never read here
    (issue #597)."""
    return __get_metrics_client(client_id=client_id)


def get_metrics(token: str) -> celaut_pb2.Metrics:
    """
    Retrieve metrics based on the provided instance token.

    The token is the one of a local instance, or the alias of a delegated one. A
    ``client_id`` is not a token: it is answered by ``get_client_metrics``, and here it
    is an invalid token (issue #597).

    :param token: The instance token used to identify the source of metrics.
    :type token: str
    :return: A protobuf object containing the metrics retrieved.
    :rtype: celaut_pb2.Metrics
    :raises Exception: If the token is not the one of an instance.
    """
    if sc.internal_instance_exists(id=token):
        return __get_metrics_internal(id=token)

    else:
        # A delegated instance is known by the alias `delegate_execution` handed out
        # -- a random hex value -- which never contains '##'. Gating
        # this lookup on that old token format rejected every one of them, so a node
        # that re-delegated a child answered its father's metrics read with "Invalid
        # token", and the father took that for the child being gone (#442).
        delegated_token = sc.get_delegated_token_by_id(id=token)
        if not delegated_token:
            raise Exception(f'Invalid token, it should be an instance token.  token: {token}')
        token = delegated_token

        # Our own books, not the peer's. A delegated instance's deposit is held
        # here, in our MU, and spent down by the maintenance tick at the rate the
        # peer quoted; the peer's separate meter runs against this node's wholesale
        # deposit there, in the peer's MU. Answering with the peer's figure would
        # report the balance of an account that is not the client's, on a scale that
        # is not his either.
        return celaut_pb2.Metrics(
            balance=to_amount(sc.get_delegated_balance(token=token)),
            # Suspended on the peer that runs it, which is what this node reports on.
            suspended=sc.is_delegated_suspended(token=token),
        )
