from typing import Optional, Generator

from grpcbigbuffer import client as grpcbf, buffer_pb2

from protos import gateway_pb2
from src.balancers.resource_balancer.resource_balancer \
    import resource_configuration_balancer
from src.builder import build
from src.gateway.iterables.abstract_service_iterable import AbstractServiceIterable, BreakIteration
from src.manager.manager import default_initial_cost, compute_start_service_cost, compute_maintenance_cost
from src.utils.logger import LOGGER as log
from src.utils.utils import from_gas_amount, get_only_the_ip_from_context, to_gas_amount
from src.utils.env import MANAGER_ITERATION_TIME


class GetServiceEstimatedCostIterable(AbstractServiceIterable):
    # TODO check cost in other peers (use RecursionGuard to prevent infinite loops).

    cost: Optional[int] = None

    def start(self):
        log('Request for the cost of a service.')

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        try:
            selected_clause: int = resource_configuration_balancer(clauses=dict(self.configuration.resources.clause))

            initial_gas_amount: int = from_gas_amount(self.configuration.initial_gas_amount) \
                if self.configuration.HasField('initial_gas_amount') \
                else default_initial_cost(
                    father_id=self.client_id if self.client_id
                        else get_only_the_ip_from_context(context_peer=self.context.peer())
                    )
            cost: int = compute_start_service_cost(metadata=self.metadata, initial_gas_amount=initial_gas_amount)
            maintenance_cost: int = compute_maintenance_cost(
                resources=self.configuration.resources.clause[selected_clause]
            )

            log(f'Execution cost for a service is requested: '
                     f'resource -> {selected_clause}. cost -> {cost} with benefit  {0}'
                     f'and maintenance cost -> {maintenance_cost} with benefit {0}.')

            yield from grpcbf.serialize_to_buffer(
                # message_iterator=generate_estimated_cost(  # TODO implementar y utilizar este método
                #              metadata=metadata,
                #              initial_gas_amount=initial_gas_amount,
                #              config=config,
                #              log=lambda selected_clause, cost, maintenance_cost: f'Execution cost for a service is requested: '
                #                      f'resource -> {selected_clause}. cost -> {cost} with benefit  {0}'
                #                      f'and maintenance cost -> {maintenance_cost} with benefit {0}.'
                #          )
                message_iterator=gateway_pb2.EstimatedCost(
                    cost=to_gas_amount(cost),
                    maintenance_cost=to_gas_amount(maintenance_cost),
                    maintance_seconds_loop=MANAGER_ITERATION_TIME,
                    variance=0,  # TODO dynamic variance.
                    comb_resource_selected=selected_clause
                ),
                indices=gateway_pb2.EstimatedCost
            )
        except build.UnsupportedArchitectureException as e:
            raise e
        finally:
            yield buffer_pb2.Buffer(signal=True)
            raise BreakIteration
