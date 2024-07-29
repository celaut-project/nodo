from typing import Optional, Tuple, List, Dict, Any, Generator

import grpc
from grpcbigbuffer import client as grpcbf

import protos.celaut_pb2 as celaut
from protos import gateway_pb2, gateway_pb2_grpc
from protos.gateway_pb2_grpcbf import StartService_input_indices
from src.balancers.estimated_cost_sorter.estimated_cost_sorter import estimated_cost_sorter
from src.builder import build
from src.manager.manager import default_initial_cost, get_client_id_on_other_peer
from src.utils import logger as l
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils.env import SEND_ONLY_HASHES_ASKING_COST, EXTERNAL_COST_TIMEOUT
from src.utils.utils import from_gas_amount, service_extended, peers_id_iterator, \
    generate_uris_by_peer_id


def service_balancer(
        metadata: celaut.Any.Metadata,
        ignore_network: str = None,
        config: Optional[gateway_pb2.Configuration] = None,
        recursion_guard_token: str = None,
) -> Generator[tuple[str, gateway_pb2.EstimatedCost], None, None]:
    # sorted by cost, tuple of celaut.Instances or 'local' , cost and clause of combination resources selected
    peers: Dict[str, gateway_pb2.EstimatedCost] = {}

    initial_gas_amount: int = from_gas_amount(config.initial_gas_amount) \
        if config.HasField("initial_gas_amount") else default_initial_cost()
    # TODO If there is noting on meta. Need to check the architecture on the buffer and write it on metadata.

    try:
        peers['local'] = generate_estimated_cost(
                metadata=metadata,
                initial_gas_amount=initial_gas_amount,
                config=config
            )
    except build.UnsupportedArchitectureException as e:
        l.LOGGER(e.__str__())
        pass
    except Exception as e:
        l.LOGGER('Error getting the local cost ' + str(e))
        raise e

    try:
        for peer_id in peers_id_iterator(ignore_network=ignore_network):
            l.LOGGER('Check cost on peer ' + peer_id)
            # TODO could use async or concurrency
            try:
                peers[peer_id] = next(grpcbf.client_grpc(
                        method=gateway_pb2_grpc.GatewayStub(
                            grpc.insecure_channel(
                                next(generate_uris_by_peer_id(peer_id))
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
                            client_id=get_client_id_on_other_peer(peer_id=peer_id),
                            recursion_guard_token=recursion_guard_token
                        ),
                        # TODO añadir initial_gas_amount y el resto de la configuracion inicial,
                        #  si es que se especifica.
                    ))
            except Exception as e:
                l.LOGGER('Error taking the cost on ' + peer_id + ' : ' + str(e))
    except Exception as e:
        l.LOGGER('Error iterating peers on service balancer ->>' + str(e))

    try:
        return estimated_cost_sorter(
                estimated_costs=peers,
                weight_clauses={_id: clause.cost_weight for _id, clause in config.resources.clause.items()}
            )
    except Exception as e:
        l.LOGGER('Error during balancer, ' + str(e))
        raise StopIteration
