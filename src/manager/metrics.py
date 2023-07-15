import grpc
from grpcbigbuffer import client as grpcbf

from protos import gateway_pb2, gateway_pb2_grpc

from src.manager.manager import generate_client_id_in_other_peer
from src.manager.system_cache import SystemCache

from src.utils.env import DOCKER_NETWORK
from src.utils.utils import from_gas_amount, is_peer_available, get_network_name, to_gas_amount, \
    generate_uris_by_peer_id
from src.utils import logger as l


sc = SystemCache()

def __get_metrics_client(client_id) -> gateway_pb2.Metrics:
    return gateway_pb2.Metrics(
        gas_amount=to_gas_amount(sc.clients[client_id].gas),
    )


def __get_metrics_internal(token: str) -> gateway_pb2.Metrics:
    return gateway_pb2.Metrics(
        gas_amount=to_gas_amount(sc.system_cache[token]['gas']),
    )


def __get_metrics_external(peer_id: str, token: str) -> gateway_pb2.Metrics:
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


# Return the integer gas amount of this node on other peer.
def gas_amount_on_other_peer(peer_id: str) -> int:
    while True:
        try:
            return from_gas_amount(
                __get_metrics_external(
                    peer_id=peer_id,
                    token=generate_client_id_in_other_peer(peer_id=peer_id)
                ).gas_amount
            )
        except Exception as e:
            l.LOGGER('Error getting gas amount from ' + peer_id + '.')
            if is_peer_available(peer_id=peer_id):
                l.LOGGER('It is assumed that the client was invalid on peer ' + peer_id)
                sc.cache_locks.delete(peer_id)
                del sc.clients_on_other_peers[peer_id]
            else:
                break


def get_metrics(token: str) -> gateway_pb2.Metrics:
    if token in sc.clients:
        return __get_metrics_client(client_id=token)

    elif '##' not in token:
        raise Exception('Invalid token, it should be a client_id or a token with ##.')

    elif get_network_name(
            ip_or_uri=token.split('##')[1],

    ) == DOCKER_NETWORK:
        return __get_metrics_internal(token=token)

    else:
        return __get_metrics_external(
            peer_id=token.split('##')[1],  # peer_id
            token=sc.external_token_hash_map[token.split['##'][2]]  # Por si el token comienza en # ...
        )