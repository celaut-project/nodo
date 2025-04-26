from typing import Optional, Generator

from bee_rpc import client as bee, buffer_pb2

from protos import gateway_pb2
from src.utils.tools.recursion_guard import RecursionGuard
from src.virtualizers.docker import build
from src.gateway.iterables.abstract_input_service_iterable import AbstractInputServiceIterable, BreakIteration
from src.manager.manager import default_initial_cost
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils.logger import LOGGER as log
from src.utils.utils import from_gas_amount, get_only_the_ip_from_context


class GetServiceEstimatedCostIterable(AbstractInputServiceIterable):
    
    # https://github.com/celaut-project/nodo/issues/70
    
    # Although this call does not perform recursion on the peers (it only returns the local estimated cost and not that of the peers), the estimated cost could be requested by the peer that was just asked to execute the service.

    cost: Optional[int] = None

    def start(self):
        log('Request for the cost of a service.')
        return super().start()

    def generate(self) -> Generator[buffer_pb2.Buffer, None, None]:
        with RecursionGuard(
                token=self.recursion_guard_token,
                generate=True
        ) as recursion_guard_token:
            try:
                initial_gas_amount: int = from_gas_amount(self.configuration.initial_gas_amount) \
                    if self.configuration.HasField('initial_gas_amount') \
                    else default_initial_cost(
                        father_id=self.client_id if self.client_id
                            else get_only_the_ip_from_context(context_peer=self.context.peer())
                        )

                yield from bee.serialize_to_buffer(
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

    def final(self):
        log('End request for the cost of a service.')
        return super().final()
