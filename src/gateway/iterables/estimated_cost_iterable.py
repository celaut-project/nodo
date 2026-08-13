from src.utils.utils import read_service_from_disk
from typing import Optional, Generator

from bee_rpc import client as bee, buffer_pb2

from protos import celaut_pb2
from src.utils.tools.recursion_guard import RecursionGuard
from src.virtualizers.architecture import UnsupportedArchitectureException
from src.gateway.iterables.abstract_input_service_iterable import AbstractInputServiceIterable, BreakIteration
from src.manager.manager import default_initial_balance
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils.logger import LOGGER as logger
from src.utils.utils import from_amount, get_only_the_ip_from_context, to_amount


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

                if not self.service_hash:
                    raise Exception("No service hash provided.")

                service = read_service_from_disk(service_hash=self.service_hash)

                if not service:
                    raise Exception(f"Service {self.service_hash} not on local registry.")

                resources = service.container.resources
                del service

                # Needs the requested resources to price, so it happens here rather than
                # before the service is read.
                if not self.configuration.HasField('initial_mu') or not self.configuration.initial_mu:
                    self.configuration.initial_mu.CopyFrom(
                        to_amount(default_initial_balance(
                            system_resources=resources.at_init,
                            service_hash=self.service_hash,
                        ))
                    )

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
