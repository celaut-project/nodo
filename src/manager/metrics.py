import grpc
from grpcbigbuffer import client as grpcbf

from protos import gateway_pb2, gateway_pb2_grpc

from src.manager.manager import generate_client_id_in_other_peer
from src.manager.system_cache import SystemCache

from src.utils.env import DOCKER_NETWORK
from src.utils.utils import from_gas_amount, is_peer_available, get_network_name, to_gas_amount, \
    generate_uris_by_peer_id
from src.utils.logger import LOGGER as log

sc = SystemCache()


def __get_metrics_client(client_id: str) -> gateway_pb2.Metrics:
    """
    Retrieve metrics for a specific client from cached data.

    This function retrieves metrics for a specific client from cached data based on the provided client ID.

    :param client_id: The ID of the client for which to retrieve metrics.
    :type client_id: str
    :return: A protobuf object containing the metrics for the specified client.
    :rtype: gateway_pb2.Metrics
    :raises KeyError: If the provided client ID does not exist in the cached data.
    """
    return gateway_pb2.Metrics(
        gas_amount=to_gas_amount(sc.clients[client_id].gas),
    )


def __get_metrics_internal(token: str) -> gateway_pb2.Metrics:
    """
    Retrieve internal metrics using cached data.

    This function retrieves internal metrics from cached data based on the provided token.

    :param token: The token used to identify the source of internal metrics.
    :type token: str
    :return: A protobuf object containing the internal metrics retrieved.
    :rtype: gateway_pb2.Metrics
    :raises KeyError: If the provided token does not exist in the cached data.
    """
    return gateway_pb2.Metrics(
        gas_amount=to_gas_amount(sc.system_cache[token]['gas']),
    )


def __get_metrics_external(peer_id: str, token: str) -> gateway_pb2.Metrics:
    """
    Retrieve external metrics using gRPC communication.

    This function retrieves metrics from an external peer using gRPC communication.

    :param peer_id: The identifier of the external peer from which to retrieve the metrics.
    :type peer_id: str
    :param token: The token used to authenticate the request and retrieve the metrics.
    :type token: str
    :return: A protobuf object containing the external metrics retrieved.
    :rtype: gateway_pb2.Metrics
    """
    return next(grpcbf.client_grpc(
        method=gateway_pb2_grpc.GatewayStub(
            grpc.insecure_channel(
                next(generate_uris_by_peer_id(peer_id=peer_id))
            )
        ).GetMetrics,
        input=gateway_pb2.TokenMessage(
            token=token
        ),
        indices_parser=gateway_pb2.Metrics,
        partitions_message_mode_parser=True
    ))


def gas_amount_on_other_peer(peer_id: str) -> int:
    """
    Retrieve the gas amount from another peer.

    This function fetches the gas amount from a specified peer's client and returns it.

    :param peer_id: The identifier of the peer from which to retrieve the gas amount.
    :type peer_id: str
    :return: The gas amount retrieved from the peer. If an error occurs, returns 0.
    :rtype: int
    :raises Exception: If an error occurs while fetching the gas amount.
    """
    try:
        return from_gas_amount(
            __get_metrics_external(
                peer_id=peer_id,
                token=generate_client_id_in_other_peer(peer_id=peer_id)
            ).gas_amount
        )
    finally:
        log('Error getting gas amount from ' + peer_id + '.')
        if is_peer_available(peer_id=peer_id):
            log('It is assumed that the client was invalid on peer ' + peer_id)
            try:
                sc.cache_locks.delete(peer_id)
                del sc.clients_on_other_peers[peer_id]
            finally:
                pass
        return 0


def get_metrics(token: str) -> gateway_pb2.Metrics:
    class InvalidTokenException(Exception):
        def __str__(self):
            return 'Invalid token, it should be a client_id or a token with ##.'

    """
    Retrieve metrics based on the provided token.

    This function retrieves metrics associated with a given token. The token can be either a client ID
    or a specialized token with specific formatting.

    :param token: The token used to identify the source of metrics.
    :type token: str
    :return: A protobuf object containing the metrics retrieved.
    :rtype: gateway_pb2.Metrics
    :raises InvalidTokenException: If the token format is invalid.
    :raises Exception: If an error occurs during the metric retrieval process.
    """
    if token in sc.clients:
        return __get_metrics_client(client_id=token)

    elif '##' not in token:
        raise InvalidTokenException()

    elif get_network_name(
            ip_or_uri=token.split('##')[1],

    ) == DOCKER_NETWORK:
        return __get_metrics_internal(token=token)

    else:
        return __get_metrics_external(
            peer_id=token.split('##')[1],  # peer_id
            token=sc.external_token_hash_map[token.split('##')[2]]  # If the token starts with ## ...
        )
