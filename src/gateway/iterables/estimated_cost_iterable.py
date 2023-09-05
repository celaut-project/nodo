from typing import Optional, Generator

from grpcbigbuffer import client as grpcbf, buffer_pb2

from protos import gateway_pb2
from src.builder import build
from src.gateway.iterables.abstract_service_iterable import AbstractServiceIterable, BreakIteration
from src.manager.manager import default_initial_cost
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils.logger import LOGGER as log
from src.utils.utils import from_gas_amount, get_only_the_ip_from_context


class GetServiceEstimatedCostIterable(AbstractServiceIterable):
    # TODO check cost in other peers (use RecursionGuard to prevent infinite loops).

    cost: Optional[int] = None

    def start(self):
        log('Request for the cost of a service.')

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        try:
            initial_gas_amount: int = from_gas_amount(self.configuration.initial_gas_amount) \
                if self.configuration.HasField('initial_gas_amount') \
                else default_initial_cost(
                    father_id=self.client_id if self.client_id
                        else get_only_the_ip_from_context(context_peer=self.context.peer())
                    )

            yield from grpcbf.serialize_to_buffer(
                message_iterator=generate_estimated_cost(
                    metadata=self.metadata,
                    initial_gas_amount=initial_gas_amount,
                    config=self.configuration
                ),
                indices=gateway_pb2.EstimatedCost
            )
        except build.UnsupportedArchitectureException as e:
            raise e
        finally:
            yield buffer_pb2.Buffer(signal=True)
            raise BreakIteration
