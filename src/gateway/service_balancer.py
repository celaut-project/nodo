from protos import gateway_pb2, gateway_pb2_grpc
from protos.gateway_pb2_grpcbf import GetServiceEstimatedCost_input, StartService_input_partitions_v2
from src import utils as l
import protos.celaut_pb2 as celaut
from typing import Dict

from src.gateway.gateway import SEND_ONLY_HASHES_ASKING_COST
from src.manager.manager import COST_AVERAGE_VARIATION, default_initial_cost, GAS_COST_FACTOR, execution_cost, \
    generate_client_id_in_other_peer
from src.utils.utils import from_gas_amount, to_gas_amount, peers_id_iterator, generate_uris_by_peer_id, \
    service_extended
from src.builder import build
import grpcbigbuffer as grpcbf

def service_balancer(
        service_buffer: bytes,
        metadata: celaut.Any.Metadata,
        ignore_network: str = None,
        initial_gas_amount: int = None,
        recursion_guard_token: str = None,
) -> Dict[str, int]:  # sorted by cost, dict of celaut.Instances or 'local'  and cost.
    class PeerCostList:
        # Sorts the list from the element with the smallest weight to the element with the largest weight.

        def __init__(self) -> None:
            self.dict: dict = {}  # elem : weight

        def add_elem(self, weight: gateway_pb2.EstimatedCost, elem: str = 'local') -> None:
            l.LOGGER('    adding elem ' + elem + ' with weight ' + str(weight.cost))
            self.dict.update({
                elem: int(from_gas_amount(weight.cost) * (1 + weight.variance * COST_AVERAGE_VARIATION))
            })

        def get(self) -> Dict[str, int]:
            return {k: v for k, v in sorted(self.dict.items(), key=lambda item: item[1])}

    peers: PeerCostList = PeerCostList()
    initial_gas_amount: int = initial_gas_amount if initial_gas_amount else default_initial_cost()
    # TODO If there is noting on meta. Need to check the architecture on the buffer and write it on metadata.
    try:
        peers.add_elem(
            weight=gateway_pb2.EstimatedCost(
                cost=to_gas_amount(execution_cost(service_buffer=service_buffer,
                                                        metadata=metadata) * GAS_COST_FACTOR + initial_gas_amount),
                variance=0
            )
        )
    except build.UnsupportedArquitectureException:
        l.LOGGER('UNSUPPORTED ARQUITECTURE.')
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
                    weight=next(grpcbf.client_grpc(
                        method=gateway_pb2_grpc.GatewayStub(
                            grpc.insecure_channel(
                                next(generate_uris_by_peer_id(peer))
                            )
                        ).GetServiceEstimatedCost,
                        indices_parser=gateway_pb2.EstimatedCost,
                        partitions_message_mode_parser=True,
                        indices_serializer=GetServiceEstimatedCost_input,
                        partitions_serializer=StartService_input_partitions_v2,
                        input=service_extended(
                            service_buffer=service_buffer,
                            metadata=metadata,
                            send_only_hashes=SEND_ONLY_HASHES_ASKING_COST,
                            initial_gas_amount=initial_gas_amount,
                            client_id=generate_client_id_in_other_peer(peer_id=peer),
                            recursion_guard_token=recursion_guard_token
                        ),
                        # TODO añadir initial_gas_amount y el resto de la configuracion inicial, si es que se especifica.
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
        return {}