from typing import Dict, Optional, Tuple, List

import grpc
from grpcbigbuffer import client as grpcbf

import protos.celaut_pb2 as celaut
from protos import gateway_pb2, gateway_pb2_grpc
from protos.gateway_pb2_grpcbf import StartService_input_indices
from src.balancers.resource_balancer.resource_balancer \
    import ClauseResource, resource_configuration_balancer
from src.balancers.resource_balancer.variance_cost_normalization import variance_cost_normalization
from src.builder import build
from src.manager.manager import default_initial_cost, generate_client_id_in_other_peer, start_service_cost
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
    class PeerCostList:
        # Sorts the list from the element with the smallest weight to the element with the largest weight.

        def __init__(self) -> None:
            self.elem_costs: Dict[str, int] = {}  # elem : cost
            self.elem_resources: Dict[str, int] = {}  # elem : clause id
            self.clauses: Dict[int, ClauseResource] = config.resources.clause.items()

        def add_elem(self, estim_cost: gateway_pb2.EstimatedCost, elem: str = 'local') -> None:
            l.LOGGER('    adding elem ' + elem + ' with weight ' + str(estim_cost.cost))

            self.elem_costs.update({
                elem: variance_cost_normalization(cost=from_gas_amount(estim_cost.cost), variance=estim_cost.variance)
            })

            self.elem_resources.update({elem: estim_cost.comb_resource_selected})

        def get(self) -> List[Tuple[str, int, int]]:
            return [(k, v, self.elem_resources[k]) for k, v in
                    sorted(self.elem_costs.items(),
                           key=lambda item: self.clauses[self.elem_resources[item[0]]].cost_weight / item[1],
                           reverse=True
                           )
                    ]

    peers: PeerCostList = PeerCostList()
    initial_gas_amount: int = from_gas_amount(config.initial_gas_amount) \
        if config.HasField("initial_gas_amount") else default_initial_cost()
    # TODO If there is noting on meta. Need to check the architecture on the buffer and write it on metadata.
    try:
        peers.add_elem(
            estim_cost=gateway_pb2.EstimatedCost(
                cost=to_gas_amount(
                    gas_amount=start_service_cost(metadata=metadata, initial_gas_amount=initial_gas_amount)
                ),
                variance=0,
                comb_resource_selected=resource_configuration_balancer(config.resources.clause.items())
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
