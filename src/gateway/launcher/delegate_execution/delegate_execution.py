from hashlib import sha256
from typing import Callable, List

import grpc
from grpcbigbuffer import client as grpcbf

import src.utils.utils
from protos import gateway_pb2, gateway_pb2_grpc
from protos.gateway_pb2_grpcbf import StartService_input_indices
from src.manager.manager import generate_client_id_in_other_peer
from src.manager.metrics import gas_amount_on_other_peer
from src.manager.system_cache import SystemCache
from src.payment_system.payment_process import increase_deposit_on_peer
from src.utils import utils, logger as l


def delegate_execution(
                        peer: str, father_id: str,
                        cost: int, metadata, config,
                        recursion_guard_token,
                        refund_gas: List[Callable]
                   ) -> gateway_pb2.Instance:
    try:
        l.LOGGER('El servicio se lanza en el nodo ' + str(peer))

        if gas_amount_on_other_peer(
                peer_id=peer,
        ) <= cost and not increase_deposit_on_peer(
            peer_id=peer,
            amount=cost
        ):
            raise Exception(
                'Launch service error increasing deposit on ' + peer + 'when it didn\'t have enough '
                                                                       'gas.')

        l.LOGGER('Spent gas, go to launch the service on ' + str(peer))
        service_instance = next(grpcbf.client_grpc(
            method=gateway_pb2_grpc.GatewayStub(
                grpc.insecure_channel(
                    next(src.utils.utils.generate_uris_by_peer_id(peer))
                )
            ).StartService,  # TODO An timeout should be implemented when requesting a service.
            partitions_message_mode_parser=True,
            indices_serializer=StartService_input_indices,
            indices_parser=gateway_pb2.Instance,
            input=utils.service_extended(
                metadata=metadata,
                config=config,
                # TODO: Could pass only the previously selected configuration with the estimate cost
                #  request, now is allowing to select another (that could be reasonable).
                client_id=generate_client_id_in_other_peer(peer_id=peer),
                recursion_guard_token=recursion_guard_token
            )
        ))
        encrypted_external_token: str = sha256(service_instance.token.encode('utf-8')).hexdigest()
        SystemCache().set_external_on_cache(
            client_id=father_id,  # Client_id
            peer_id=peer,  # Add node_uri.
            encrypted_external_token=encrypted_external_token,  # Add token.
            external_token=service_instance.token
        )
        service_instance.token = father_id + '##' + peer + '##' + encrypted_external_token
        # TODO adapt for ipv6 too.
        return service_instance
    except Exception as e:
        l.LOGGER('Failed starting a service on peer, occurs the error: ' + str(e))
        try:
            refund_gas.pop()()
        except IndexError:
            pass
