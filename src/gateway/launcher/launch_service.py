from typing import Optional

from protos import celaut_pb2 as celaut, gateway_pb2
from src.balancers.service_balancer.service_balancer import service_balancer
from src.gateway.launcher.delegate_execution.delegate_execution import delegate_execution
from src.gateway.launcher.local_execution.local_execution import local_execution
from src.manager.manager import spend_gas
from src.utils import utils, logger as l
from src.utils.env import IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER, \
    DOCKER_NETWORK
from src.utils.tools.recursion_guard import RecursionGuard
from src.utils.utils import from_gas_amount


def launch_service(
        service: celaut.Service,
        metadata: celaut.Any.Metadata,
        father_ip: str,
        father_id: str = None,
        service_id: str = None,
        config: Optional[gateway_pb2.Configuration] = None,
        recursion_guard_token: str = None,
) -> gateway_pb2.Instance:
    l.LOGGER('Go to launch a service. ')
    if service is None:
        raise Exception("Service object can't be None")

    with RecursionGuard(
            token=recursion_guard_token,
            generate=bool(father_id)  # Use only if is from outside.
    ) as recursion_guard_token:

        if not father_id:
            father_id = father_ip
        if father_ip == father_id and utils.get_network_name(father_ip) != DOCKER_NETWORK:
            raise Exception('Client id not provided.')

        for peer, estimated_cost in service_balancer(
                metadata=metadata,
                ignore_network=utils.get_network_name(
                    ip_or_uri=father_ip
                ) if IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER else None,
                config=config,
                recursion_guard_token=recursion_guard_token
        ):
            try:
                l.LOGGER(f'Service balancer select peer {peer}')

                refund_gas = []

                if not spend_gas(
                        id=father_id,
                        gas_to_spend=from_gas_amount(estimated_cost.cost),
                        refund_gas_function_container=refund_gas
                ):
                    raise Exception('Launch service error spending gas for ' + father_id)

                # Delegate the service instance execution.
                if peer != 'local':
                    return delegate_execution(
                        peer=peer, father_id=father_id,
                        cost=from_gas_amount(estimated_cost.cost), metadata=metadata, config=config,
                        recursion_guard_token=recursion_guard_token,
                        refund_gas=refund_gas
                    )

                else:
                    return local_execution(
                        config=config, resources=config.resources.clause[estimated_cost.comb_resource_selected],
                        father_id=father_id, father_ip=father_ip,
                        metadata=metadata, service=service, service_id=service_id,
                        refund_gas=refund_gas
                    )

            except Exception as e:
               l.LOGGER(f"Exception launching service on peer {peer}: {str(e)}")
               continue

        _err_msg = f"Can't launch this service {service_id}"
        l.LOGGER(_err_msg)
        raise Exception(_err_msg)
