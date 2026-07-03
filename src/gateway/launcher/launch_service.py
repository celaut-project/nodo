from typing import Optional
import traceback

from protos import celaut_pb2 as celaut, celaut_pb2
from src.balancers.execution_balancer.execution_balancer import execution_balancer
from src.gateway.launcher.delegate_execution.delegate_execution import delegate_execution
from src.gateway.launcher.local_execution.local_execution import local_execution
from src.manager.manager import default_initial_cost, spend_gas
from src.utils import utils, logger as log
from src.utils.tools.recursion_guard import RecursionGuard
from src.utils.utils import from_gas_amount, to_gas_amount
from src.database.sql_connection import SQLConnection
from src.virtualizers.firewall import allow_connection_to_instance
from src.utils.cost_functions.generate_estimated_cost import (
    generate_estimated_cost,
    get_resource_availability,
)
from src.virtualizers.architecture import UnsupportedArchitectureException

sc = SQLConnection()


def _detect_local_preflight_failure(
        service: celaut.Service,
        metadata: celaut.Metadata,
        configuration: celaut_pb2.Configuration,
) -> Optional[str]:
    try:
        estimated_cost = generate_estimated_cost(
            resources=service.container.resources,
            metadata=metadata,
            config=configuration,
        )
        if estimated_cost:
            return None

        availability = get_resource_availability(service.container.resources)
        return availability.get("reason") or "Local execution was rejected due to insufficient resources."
    except UnsupportedArchitectureException as exc:
        return str(exc)
    except Exception as exc:
        return f"Local preflight failed with {type(exc).__name__}: {exc}"


def _format_launch_failure(
        service_id: Optional[str],
        launch_failures: list[str],
        local_preflight_failure: Optional[str] = None,
) -> str:
    details = []
    if local_preflight_failure:
        details.append(f"local preflight: {local_preflight_failure}")
    details.extend(launch_failures)

    if not details:
        return (
            f"Unable to launch service {service_id}: no eligible local executor or peer "
            f"was available at this time."
        )

    return f"Unable to launch service {service_id}. Attempt details: {' | '.join(details[:8])}"


def launch_service(
        service: celaut.Service,
        metadata: celaut.Metadata,
        father_ip: str,
        father_id: Optional[str] = None,
        service_id: str = None,
        configuration: Optional[celaut_pb2.Configuration] = None,
        recursion_guard_token: str = None,
) -> celaut_pb2.ServiceInstance:

    with RecursionGuard(
            token=recursion_guard_token,
            generate=bool(father_id)  # Use only if is from outside.
    ) as recursion_guard_token:

        # Check father id.
        if not father_id:
            father_id = sc.get_local_instance_id_by_uri(father_ip)
            if not father_id:
                raise Exception('Client id not provided.')
            else:
                log.LOGGER(f"Service launch request made by the service {father_id}.")
        else:
            log.LOGGER(f"Service launch request made by the client {father_id}.")

        # Check configuration
        if not configuration:
            configuration = celaut_pb2.Configuration()

        if not configuration.HasField('initial_gas_amount') or not configuration.initial_gas_amount:
            configuration.initial_gas_amount.CopyFrom(to_gas_amount(default_initial_cost()))

        local_preflight_failure = _detect_local_preflight_failure(
            service=service,
            metadata=metadata,
            configuration=configuration,
        )
        launch_failures = []
        local_attempted = False

        for peer, estimated_cost in execution_balancer(
                resources=service.container.resources,
                service_id=service_id,
                metadata=metadata,
                ignore_network=utils.get_network_name(direction=father_ip),
                configuration=configuration,
                recursion_guard_token=recursion_guard_token,
                service=service
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
                        cost=from_gas_amount(estimated_cost.cost), 
                        metadata=metadata, 
                        config=configuration,
                        recursion_guard_token=recursion_guard_token,
                        refund_gas=refund_gas
                    )

                else:
                    local_attempted = True
                    instance = local_execution(
                        config=configuration,
                        resources=service.container.resources,
                        father_id=father_id, father_ip=father_ip,
                        metadata=metadata, service=service, service_id=service_id,
                        refund_gas=refund_gas
                    )

                #  If the service was from an internal instance, allow it to connect to it's new dependency.
                #   There is an exception for instances with the "rundev" refix. In such cases there is no container.
                if "rundev" not in father_id and sc.internal_instance_exists(id=father_id):
                    try:
                        if not allow_connection_to_instance(
                            vmachine_id=father_id,
                            instance=instance.instance,
                        ):
                            log.LOGGER(
                                f"Firewall allow_connection_to_instance failed for parent instance {father_id}"
                            )
                            # TODO This should be controlled.
                    except Exception as e:
                        log.LOGGER(f"Exception blocking firewall rules to {father_id} for the dependency {str(instance)}")
                        raise e

                return instance

            except Exception as e:
               log.LOGGER(f"Exception launching service on peer {peer}: {str(e)}")
               log.LOGGER(traceback.format_exc())
               launch_failures.append(f"{peer}: {type(e).__name__}: {e}")
               continue

        if local_attempted:
            local_preflight_failure = None

        _err_msg = _format_launch_failure(
            service_id=service_id,
            launch_failures=launch_failures,
            local_preflight_failure=local_preflight_failure,
        )
        log.LOGGER(_err_msg)
        raise Exception(_err_msg)
