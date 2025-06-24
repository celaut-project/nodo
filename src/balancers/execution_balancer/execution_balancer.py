from typing import Dict, Generator

import grpc
from bee_rpc import client as bee

import protos.celaut_pb2 as celaut
from protos import celaut_pb2, celaut_pb2_grpc
from protos.gateway_bee import StartService_input_indices
from src.balancers.estimated_cost_sorter.estimated_cost_sorter import estimated_cost_sorter
from src.virtualizers.docker import build
from src.manager.manager import get_client_id_on_other_peer
from src.utils import logger as log
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils.utils import service_extended, peers_id_iterator, \
    generate_uris_by_peer_id
from src.utils.env import EnvManager

env_manager = EnvManager()

SEND_ONLY_HASHES_ASKING_COST = env_manager.get_env("SEND_ONLY_HASHES_ASKING_COST")
EXTERNAL_COST_TIMEOUT = env_manager.get_env("EXTERNAL_COST_TIMEOUT")

def __pretty_format_peers(peers: dict[str, celaut_pb2.EstimatedCost]) -> str:
    
    # Formats an EstimatedCost proto directly, no JSON or extra fields.
    def format_estimated_cost_simple(cost_proto) -> str:
        fields = []
        if hasattr(cost_proto, 'cost') and hasattr(cost_proto.cost, 'n'):
            fields.append(f"cost: {cost_proto.cost.n}")
        if hasattr(cost_proto, 'min_maintenance_cost') and hasattr(cost_proto.min_maintenance_cost, 'n'):
            fields.append(f"min_maintenance_cost: {cost_proto.min_maintenance_cost.n}")
        if hasattr(cost_proto, 'max_maintenance_cost') and hasattr(cost_proto.max_maintenance_cost, 'n'):
            fields.append(f"max_maintenance_cost: {cost_proto.max_maintenance_cost.n}")
        fields += [
            f"maintenance_seconds_loop: {cost_proto.maintenance_seconds_loop}",
            f"variance: {cost_proto.variance}",
            f"comb_resource_selected: {cost_proto.comb_resource_selected}"
        ]
        return "\n" + "\n".join(f"    {line}" for line in fields)
    
    lines = ["Collected execution costs:"]
    lines += [f"- Peer {peer_id}:{format_estimated_cost_simple(cost_proto)}" for peer_id, cost_proto in peers.items()]
    return "\n".join(lines)

def execution_balancer(
        service_id: str,
        resources: celaut.Service.Container.Resources,
        metadata: celaut.Metadata,
        configuration: celaut_pb2.Configuration,
        ignore_network: str = None,
        recursion_guard_token: str = None,
) -> Generator[tuple[str, celaut_pb2.EstimatedCost], None, None]:
    
    # sorted by cost, tuple of celaut.Instances or 'local' , cost and clause of combination resources selected
    peers: Dict[str, celaut_pb2.EstimatedCost] = {}
    
    # TODO If there is noting on meta. Need to check the architecture on the buffer and write it on metadata.

    try:
        peers['local'] = generate_estimated_cost(
            resources=resources,
            metadata=metadata,
            config=configuration
        )
    except build.UnsupportedArchitectureException as e:
        log.LOGGER(e.__str__())
        pass
    except Exception as e:
        log.LOGGER('Error getting the local cost ' + str(e))
        raise e

    try:
        for peer_id in peers_id_iterator(ignore_network=ignore_network):
            log.LOGGER('Check cost on peer ' + peer_id)
            # TODO could use async or concurrency
            try:
                peers[peer_id] = next(bee.client_grpc(
                        method=celaut_pb2_grpc.GatewayStub(
                            grpc.insecure_channel(
                                next(generate_uris_by_peer_id(peer_id))
                            )
                        ).GetServiceEstimatedCost,
                        indices_parser=celaut_pb2.EstimatedCost,
                        timeout=EXTERNAL_COST_TIMEOUT,
                        partitions_message_mode_parser=True,
                        indices_serializer=StartService_input_indices,
                        input=service_extended(
                            config=configuration,
                            metadata=metadata,
                            send_only_hashes=SEND_ONLY_HASHES_ASKING_COST,
                            client_id=get_client_id_on_other_peer(peer_id=peer_id),
                            recursion_guard_token=recursion_guard_token
                        ),
                        # TODO: add initial_gas_amount and the rest of the initial configuration, if it is specified.
                    ))
            except Exception as e:
                log.LOGGER('Exception taking the cost for ' + peer_id + ': ' + str(e) + " (maybe it doesn't have the service)")
    except Exception as e:
        log.LOGGER('Error iterating peers on service balancer:' + str(e))

    try:
        log.LOGGER(f"Collected costs of execution {__pretty_format_peers(peers)}")
        return estimated_cost_sorter(estimated_costs=peers)
    except Exception as e:
        log.LOGGER('Error during estimated cost sorter on execution balancer:' + str(e))
        raise StopIteration
