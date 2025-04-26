from typing import Optional

from protos import celaut_pb2 as celaut, gateway_pb2
from src.balancers.execution_balancer.execution_balancer import execution_balancer
from src.gateway.launcher.delegate_execution.delegate_execution import delegate_execution
from src.gateway.launcher.local_execution.local_execution import local_execution
from src.manager.manager import spend_gas
from src.utils import utils, logger as log
from src.utils.tools.recursion_guard import RecursionGuard
from src.utils.utils import from_gas_amount
from src.database.sql_connection import SQLConnection
from src.virtualizers.docker.firewall import Protocol, allow_connection

sc = SQLConnection()


def launch_service(
        service: celaut.Service,
        metadata: celaut.Metadata,
        father_ip: str,
        father_id: Optional[str] = None,
        service_id: str = None,
        config: Optional[gateway_pb2.Configuration] = None,
        recursion_guard_token: str = None,
) -> gateway_pb2.Instance:
    log.LOGGER('Go to launch a service. ')
    if service is None:
        raise Exception("Service object can't be None")

    with RecursionGuard(
            token=recursion_guard_token,
            generate=bool(father_id)  # Use only if is from outside.
    ) as recursion_guard_token:

        if not father_id:
            father_id = sc.get_local_instance_id_by_uri(father_ip)
            if not father_id:
                raise Exception('Client id not provided.')
            else:
                log.LOGGER(f"Service launch request made by the service {father_id}.")
        else:
            log.LOGGER(f"Service launch request made by the client {father_id}.")

        for peer, estimated_cost in execution_balancer(
                metadata=metadata,
                ignore_network=utils.get_network_name(direction=father_ip),
                config=config,
                recursion_guard_token=recursion_guard_token
        ):
            try:
                log.LOGGER(f'Service balancer select peer {peer}')

                refund_gas = []

                if not spend_gas(
                        id=father_id,
                        gas_to_spend=from_gas_amount(estimated_cost.cost),
                        refund_gas_function_container=refund_gas
                ):
                    raise Exception('Launch service error spending gas for ' + father_id)

                # Delegate the service instance execution.
                if peer != 'local':
                    instance = delegate_execution(
                        service_id=service_id,
                        peer=peer, 
                        father_id=father_id,
                        cost=from_gas_amount(estimated_cost.cost), metadata=metadata, config=config,
                        recursion_guard_token=recursion_guard_token,
                        refund_gas=refund_gas
                    )

                else:
                    instance = local_execution(
                        config=config, resources=config.resources.clause[estimated_cost.comb_resource_selected],
                        father_id=father_id, father_ip=father_ip,
                        metadata=metadata, service=service, service_id=service_id,
                        refund_gas=refund_gas
                    )

                if sc.internal_instance_exists(id=father_id):
                    try:
                        for slot in instance.instance.uri_slot:
                            for uri in slot.uri:
                                if not allow_connection(container_id=father_id,
                                                        ip=uri.ip, port=uri.port, protocol=Protocol.TCP):
                                    log.LOGGER(f"Docker firewall allow connection function failed for the father {father_id}")
                                    # TODO This should be controlled.
                    except Exception as e:
                        log.LOGGER(f"Exception blocking firewall rules to {father_id} for the dependency {str(instance)}")
                        raise e

                return instance

            except Exception as e:
               log.LOGGER(f"Exception launching service on peer {peer}: {str(e)}")
               continue

        _err_msg = f"Can't launch this service {service_id}"
        log.LOGGER(_err_msg)
        raise Exception(_err_msg)
