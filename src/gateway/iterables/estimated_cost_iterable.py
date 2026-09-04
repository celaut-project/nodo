from src.utils.utils import read_service_from_disk
from typing import Optional, Generator

from bee_rpc import client as bee, buffer_pb2

from protos import celaut_pb2
from src.utils.tools.recursion_guard import RecursionGuard
from src.virtualizers.architecture import UnsupportedArchitectureException, get_arch_tag
from src.gateway.iterables.abstract_input_service_iterable import AbstractInputServiceIterable, BreakIteration
from src.manager.manager import default_initial_balance
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils import activity_window
from src.utils.network_policy import enforce_network_policy
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

                # A price is an offer, and a closed node has nothing to offer: quoting
                # while outside `activity_window` only gets the asking peer's balancer
                # to select us and then fail at launch, the same reasoning the network
                # policy below is enforced here for (#280).
                #
                # No dev-client exemption on this path, unlike `launch_service`: this
                # RPC is only ever reached by a peer over the wire, so every request
                # arriving here is outside work by construction. The operator's own
                # launches price themselves in-process through
                # `execution_balancer`/`generate_estimated_cost` and never come past
                # here at all.
                if not activity_window.is_open():
                    raise Exception(activity_window.closed_reason())

                service = read_service_from_disk(service_hash=self.service_hash)

                if not service:
                    raise Exception(f"Service {self.service_hash} not on local registry.")

                # This node does not quote a service it would then refuse to run:
                # a price is an offer, and answering one for a service whose
                # networks the policy rejects only gets the peer's balancer to
                # select us and fail at launch (#280).
                enforce_network_policy(
                    networks=service.network,
                    subject=f"service {self.service_hash}",
                )

                # The service's architecture, which selects the memory price when the
                # operator has set one per arch. Read before the service is dropped:
                # the quote and the charge have to come from the same rate, or this
                # node offers one price and bills another.
                service_arch = get_arch_tag(service=service, metadata=self.metadata)

                resources = service.container.resources
                del service

                # Needs the requested resources to price, so it happens here rather than
                # before the service is read.
                if not self.configuration.HasField('initial_mu') or not self.configuration.initial_mu:
                    self.configuration.initial_mu.CopyFrom(
                        to_amount(default_initial_balance(
                            system_resources=resources.at_init,
                            service_hash=self.service_hash,
                            arch=service_arch,
                        ))
                    )

                yield from bee.serialize_to_buffer(
                    message_iterator=generate_estimated_cost(
                        metadata=self.metadata,
                        config=self.configuration,
                        resources=resources,
                        arch=service_arch,
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
