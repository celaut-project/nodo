from typing import Optional, Tuple, List

import grpc
from grpcbigbuffer import client as grpcbf

import protos.celaut_pb2 as celaut
from protos import gateway_pb2, gateway_pb2_grpc
from protos.gateway_pb2_grpcbf import StartService_input_indices
from src.balancers.resource_balancer.resource_balancer \
    import resource_configuration_balancer
from src.balancers.service_balancer.peer_cost_list import PeerCostList
from src.builder import build
from src.manager.manager import default_initial_cost, generate_client_id_in_other_peer, compute_start_service_cost, \
    compute_maintenance_cost
from src.utils import logger as l
from src.utils.env import SEND_ONLY_HASHES_ASKING_COST, EXTERNAL_COST_TIMEOUT
from src.utils.utils import from_gas_amount, to_gas_amount, service_extended, peers_id_iterator, \
    generate_uris_by_peer_id


def service_balancer(
        metadata: celaut.Any.Metadata,
        ignore_network: str = None,
        config: Optional[gateway_pb2.Configuration] = None,
        recursion_guard_token: str = None,
) -> List[Tuple[str, int, int]]:
    # sorted by cost, tuple of celaut.Instances or 'local' , cost and clause of combination resources selected
    peers: PeerCostList = PeerCostList(config=config)
    initial_gas_amount: int = from_gas_amount(config.initial_gas_amount) \
        if config.HasField("initial_gas_amount") else default_initial_cost()
    # TODO If there is noting on meta. Need to check the architecture on the buffer and write it on metadata.

    selected_clause: int = resource_configuration_balancer(dict(config.resources.clause))
    try:
        peers.add_elem(
            # estim_cost=generate_estimated_cost(  # TODO implementar y utilizar este método
            #     metadata=metadata,
            #     initial_gas_amount=initial_gas_amount,
            #     config=config
            # )
            estim_cost=gateway_pb2.EstimatedCost(
                cost=to_gas_amount(
                    gas_amount=compute_start_service_cost(metadata=metadata, initial_gas_amount=initial_gas_amount)
                ),
                maintenance_cost=to_gas_amount(
                    gas_amount=compute_maintenance_cost(
                        resource=compute_maintenance_cost(
                            resource=config.resources.clause[selected_clause]
                        )
                    )
                ),
                variance=0,
                comb_resource_selected=selected_clause
            )
        )
    except build.UnsupportedArchitectureException as e:
        l.LOGGER(e.__str__())
        pass
    except Exception as e:
        l.LOGGER('Error getting the local cost ' + str(e))
        raise e

    try:
        for peer in peers_id_iterator(ignore_network=ignore_network):
            l.LOGGER('Check cost on peer ' + peer)
            # TODO could use async or concurrency ¿numba?. And use timeout.
            try:
                peers.add_elem(
                    elem=peer,
                    estim_cost=next(grpcbf.client_grpc(
                        method=gateway_pb2_grpc.GatewayStub(
                            grpc.insecure_channel(
                                next(generate_uris_by_peer_id(peer))
                            )
                        ).GetServiceEstimatedCost,
                        indices_parser=gateway_pb2.EstimatedCost,
                        timeout=EXTERNAL_COST_TIMEOUT,
                        partitions_message_mode_parser=True,
                        indices_serializer=StartService_input_indices,
                        input=service_extended(
                            config=config,
                            metadata=metadata,
                            send_only_hashes=SEND_ONLY_HASHES_ASKING_COST,
                            client_id=generate_client_id_in_other_peer(peer_id=peer),
                            recursion_guard_token=recursion_guard_token
                        ),
                        # TODO añadir initial_gas_amount y el resto de la configuracion inicial,
                        #  si es que se especifica.
                    ))
                )
            except Exception as e:
                l.LOGGER('Error taking the cost on ' + peer + ' : ' + str(e))
    except Exception as e:
        l.LOGGER('Error iterating peers on service balancer ->>' + str(e))

    try:
        return peers.get()
    except Exception as e:
        l.LOGGER('Error during balancer, ' + str(e))
        return []
