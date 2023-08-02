from typing import Optional, Generator

from grpcbigbuffer import client as grpcbf, buffer_pb2

from protos import gateway_pb2
from src.builder import build
from src.gateway.iterables.abstract_service_iterable import AbstractServiceIterable, BreakIteration
from src.manager.manager import execution_cost, default_initial_cost
from src.utils import logger as l
from src.utils.env import GAS_COST_FACTOR
from src.utils.utils import from_gas_amount, get_only_the_ip_from_context, to_gas_amount


class GetServiceEstimatedCostIterable(AbstractServiceIterable):
    # TODO check cost in other peers (use RecursionGuard to prevent infinite loops).

    cost: Optional[int] = None

    def start(self):
        l.LOGGER('Request for the cost of a service.')

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        try:
            self.cost = execution_cost(
                metadata=self.metadata
            ) * GAS_COST_FACTOR
        except build.UnsupportedArchitectureException as e:
            raise e
        except Exception:
            yield buffer_pb2.Buffer(signal=True)
        else:
            raise BreakIteration

    def final(self):
        initial_service_cost = from_gas_amount(self.initial_gas_amount)
        if not initial_service_cost:
            initial_service_cost: int = default_initial_cost(
                father_id=self.client_id if self.client_id \
                    else get_only_the_ip_from_context(context_peer=self.context.peer())
            )
        cost: int = self.cost + initial_service_cost if self.cost else initial_service_cost
        l.LOGGER('Execution cost for a service is requested, cost -> ' + str(cost) + ' with benefit ' + str(0))
        if cost is None:
            raise Exception("I dont've the service.")
        for b in grpcbf.serialize_to_buffer(
                message_iterator=gateway_pb2.EstimatedCost(
                    cost=to_gas_amount(cost),
                    variance=0  # TODO dynamic variance.
                ),
                indices=gateway_pb2.EstimatedCost
        ):
            yield b


