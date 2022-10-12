from protos import gateway_pb2, gateway_pb2_grpc
from src.manager.manager import generate_client_id_in_other_peer, DOCKER_NETWORK
from src.manager.system_cache import cache_locks, clients_on_other_peers, clients, external_token_hash_map, system_cache
from src.utils.utils import from_gas_amount, is_peer_available, get_network_name, generate_uris_by_peer_id, \
    to_gas_amount

import grpc
import grpcbigbuffer as grpcbf
from src.utils import logger as l

def __get_metrics_client(client_id) -> gateway_pb2.Metrics:
    return gateway_pb2.Metrics(
        gas_amount=to_gas_amount(clients[client_id].gas),
    )


def __get_metrics_internal(token: str) -> gateway_pb2.Metrics:
    return gateway_pb2.Metrics(
        gas_amount=to_gas_amount(system_cache[token]['gas']),
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
                cache_locks.delete(peer_id)
                del clients_on_other_peers[peer_id]
            else:
                break


def get_metrics(token: str) -> gateway_pb2.Metrics:
    if token in clients:
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
            token=external_token_hash_map[token.split['##'][2]]  # Por si el token comienza en # ...
        )