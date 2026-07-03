from typing import Dict, Generator

import grpc
from bee_rpc import client as bee

import protos.celaut_pb2 as celaut
from protos import celaut_pb2, celaut_pb2_grpc
from protos.gateway_bee import StartService_input_indices
from src.balancers.estimated_cost_sorter.estimated_cost_sorter import estimated_cost_sorter
from src.virtualizers.architecture import UnsupportedArchitectureException
from src.manager.manager import get_client_id_on_other_peer
from src.utils import logger as log
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils.utils import service_extended, peers_id_iterator, \
    generate_uris_by_peer_id, read_service_from_disk
from src.utils.networks import filter_placements_for_colocation, local_node_hosts_network
from src.database.sql_connection import SQLConnection
from src.utils.config import ConfigManager

env_manager = ConfigManager()
sc = SQLConnection()

SEND_ONLY_HASHES_ASKING_COST = env_manager.get("SEND_ONLY_HASHES_ASKING_COST")
EXTERNAL_COST_TIMEOUT = env_manager.get("EXTERNAL_COST_TIMEOUT")

def __pretty_format_peers(peers: dict[str, celaut_pb2.EstimatedCost]) -> str:
    
    # Formats an EstimatedCost proto directly, no JSON or extra fields.
    def format_estimated_cost_simple(cost_proto) -> str:
        if not hasattr(cost_proto, 'cost') or \
            not hasattr(cost_proto.cost, 'n') or \
            not hasattr(cost_proto, 'init_maintenance_cost') or \
            not hasattr(cost_proto.init_maintenance_cost, 'n') or \
            not hasattr(cost_proto, 'max_maintenance_cost') or \
            not hasattr(cost_proto.max_maintenance_cost, 'n') or \
            not hasattr(cost_proto, 'maintenance_seconds_loop') or \
            not hasattr(cost_proto, 'variance'):
            log.LOGGER(f"Estimated cost is missing required fields, skipping. Estimated cost: {cost_proto}")
            return "Invalid EstimatedCost (missing required fields)"

        fields = []
        if hasattr(cost_proto, 'cost') and hasattr(cost_proto.cost, 'n'):
            fields.append(f"cost: {cost_proto.cost.n}")
        if hasattr(cost_proto, 'init_maintenance_cost') and hasattr(cost_proto.init_maintenance_cost, 'n'):
            fields.append(f"init_maintenance_cost: {cost_proto.init_maintenance_cost.n}")
        if hasattr(cost_proto, 'max_maintenance_cost') and hasattr(cost_proto.max_maintenance_cost, 'n'):
            fields.append(f"max_maintenance_cost: {cost_proto.max_maintenance_cost.n}")
        fields += [
            f"maintenance_seconds_loop: {cost_proto.maintenance_seconds_loop}",
            f"variance: {cost_proto.variance}"
        ]
        return "\n" + "\n".join(f"    {line}" for line in fields)
    
    lines = ["Collected execution costs:"]
    lines += [f"- Peer {peer_id}:{format_estimated_cost_simple(cost_proto)}" for peer_id, cost_proto in peers.items()]
    return "\n".join(lines)

def _local_hosts_network(network_id: bytes) -> bool:
    """Does this node already run an instance of the given shared-disk network?"""
    return local_node_hosts_network(
        network_id,
        local_rows=sc.get_local_instances_with_service(),
        load_service=read_service_from_disk,
    )


def execution_balancer(
        service_id: str,
        resources: celaut.Service.Container.Resources,
        metadata: celaut.Metadata,
        configuration: celaut_pb2.Configuration,
        ignore_network: str = None,
        recursion_guard_token: str = None,
        service: celaut.Service = None,
) -> Generator[tuple[str, celaut_pb2.EstimatedCost], None, None]:

    # sorted by cost, tuple of celaut.Instances or 'local' and cost
    peers: Dict[str, celaut_pb2.EstimatedCost] = {}
    
    # TODO If there is noting on meta. Need to check the architecture on the buffer and write it on metadata.

    try:
        _local = generate_estimated_cost(
            resources=resources,
            metadata=metadata,
            config=configuration
        )
        if _local:
            peers['local'] = _local
    except UnsupportedArchitectureException as e:
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

    # Co-location gating for shared-disk (virtiofs) networks: an instance that
    # declares a virtiofs network can only run where its network can be
    # co-located (same node), so drop placements that would break disk sharing.
    if service is not None:
        try:
            peers = filter_placements_for_colocation(
                service=service,
                peers=peers,
                local_hosts_network=_local_hosts_network,
                logger_fn=log.LOGGER,
            )
        except Exception as e:
            log.LOGGER('Error applying virtiofs co-location placement gating: ' + str(e))

    try:
        log.LOGGER(f"Collected costs of execution {__pretty_format_peers(peers)}")
        return estimated_cost_sorter(estimated_costs=peers)
    except Exception as e:
        log.LOGGER('Error during estimated cost sorter on execution balancer:' + str(e))
        raise StopIteration
