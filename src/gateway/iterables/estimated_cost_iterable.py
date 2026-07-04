from src.utils.utils import read_service_from_disk
from typing import Optional, Generator

from bee_rpc import client as bee, buffer_pb2

from protos import celaut_pb2
from src.utils.tools.recursion_guard import RecursionGuard
from src.virtualizers.architecture import UnsupportedArchitectureException
from src.gateway.iterables.abstract_input_service_iterable import AbstractInputServiceIterable, BreakIteration
from src.manager.manager import default_initial_cost
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils.logger import LOGGER as logger
from src.utils.networks import node_can_host_service, local_node_hosts_network
from src.utils.utils import from_gas_amount, get_only_the_ip_from_context, to_gas_amount
from src.database.sql_connection import SQLConnection

_sc = SQLConnection()


def _local_hosts_network(network_id: bytes) -> bool:
    return local_node_hosts_network(
        network_id,
        local_rows=_sc.get_local_instances_with_service(),
        load_service=read_service_from_disk,
    )


class GetServiceEstimatedCostIterable(AbstractInputServiceIterable):
    
    # https://github.com/celaut-project/nodo/issues/70
    
    # Although this call does not perform recursion on the peers (it only returns the local estimated cost and not that of the peers), the estimated cost could be requested by the peer that was just asked to execute the service.

    cost: Optional[int] = None

    def start(self):
        logger('Request for the cost of a service.')
        return super().start()

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        with RecursionGuard(
                token=self.recursion_guard_token,
                generate=True
        ) as recursion_guard_token:
            try:

                if not self.configuration:
                    self.configuration = celaut_pb2.Configuration()

                if not self.configuration.HasField('initial_gas_amount') or not self.configuration.initial_gas_amount:
                    self.configuration.initial_gas_amount.CopyFrom(to_gas_amount(default_initial_cost(
                        father_id=self.client_id if self.client_id
                            else get_only_the_ip_from_context(context_peer=self.context.peer())
                        )))

                if not self.service_hash:
                    raise Exception("No service hash provided.")

                service = read_service_from_disk(service_hash=self.service_hash)

                if not service:
                    raise Exception(f"Service {self.service_hash} not on local registry.")

                # Shared-disk (virtiofs) admissibility: decline (no cost) if this
                # node cannot host the service — a guest-only network must already
                # exist here; a seed network may run anywhere. The caller treats a
                # cost error as "this node can't run it", which is exactly the
                # co-location decision (no separate placement negotiation).
                if not node_can_host_service(service, _local_hosts_network, logger_fn=logger):
                    raise Exception(
                        "Node cannot host the service: a declared guest-only "
                        "virtiofs network is not present on this node."
                    )

                resources = service.container.resources
                del service

                yield from bee.serialize_to_buffer(
                    message_iterator=generate_estimated_cost(
                        metadata=self.metadata,
                        config=self.configuration,
                        resources=resources
                    ),
                    indices=celaut_pb2.EstimatedCost
                )
            
            except UnsupportedArchitectureException as e:
                raise e
            
            finally:
                # raise BreakIteration
                yield buffer_pb2.Buffer(signal=True)

    def final(self):
        logger('End request for the cost of a service.')
        return super().final()
