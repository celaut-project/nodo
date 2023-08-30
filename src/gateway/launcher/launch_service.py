from typing import Optional

import docker as docker_lib

from protos import celaut_pb2 as celaut, gateway_pb2
from src.balancers.service_balancer.service_balancer import service_balancer
from src.builder import build
from src.gateway.launcher.create_container import create_container
from src.gateway.launcher.delegate_execution import delegate_execution
from src.gateway.launcher.set_config import set_config
from src.manager.manager import default_initial_cost, spend_gas, \
    start_service_cost, add_container
from src.utils import utils, logger as l
from src.utils.env import IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER, \
    DOCKER_NETWORK, \
    DEFAULT_SYSTEM_RESOURCES, GAS_COST_FACTOR
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

        getting_container = False  # Here it asks the balancer if it should assign the job to a peer.

        while True:
            abort_it = True
            for peer, cost, resource_clause_id in service_balancer(
                    metadata=metadata,
                    ignore_network=utils.get_network_name(
                        ip_or_uri=father_ip
                    ) if IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER else None,
                    config=config,
                    recursion_guard_token=recursion_guard_token
            ):
                resources: gateway_pb2.CombinationResources.Clause = config.resources.clause[resource_clause_id]
                if abort_it:
                    abort_it = False
                l.LOGGER('Service balancer select peer ' + str(peer) + ' with cost ' + str(cost))

                # Delegate the service instance execution.
                if peer != 'local':
                    return delegate_execution(
                        peer=peer, father_id=father_id,
                        cost=cost, metadata=metadata, config=config,
                        recursion_guard_token=recursion_guard_token
                    )

                #  The node launches the service locally.
                if getting_container:
                    l.LOGGER('Nodo launches the service locally.')

                initial_gas_amount: int = from_gas_amount(config.initial_gas_amount) \
                    if config.HasField("initial_gas_amount") else default_initial_cost(father_id=father_id)

                initial_system_resources: celaut.Sysresources = resources.min_sysreq \
                    if resources.HasField('min_sysreq') and resources.min_sysreq else DEFAULT_SYSTEM_RESOURCES

                refund_gas = []
                if not spend_gas(
                        token_or_container_ip=father_id,
                        gas_to_spend=start_service_cost(
                            initial_gas_amount=initial_gas_amount,
                            metadata=metadata
                        ) * GAS_COST_FACTOR,
                ):
                    raise Exception('Launch service error spending gas for ' + father_id)

                try:
                    service_id = build.build(
                        service=service,
                        metadata=metadata,
                        service_id=service_id,
                        get_it=not getting_container,
                    )  # If the container is not built, build it.
                except build.UnsupportedArchitectureException as e:
                    try:
                        refund_gas.pop()()
                    except IndexError:
                        pass
                    finally:
                        raise e
                except build.WaitBuildException:
                    # If it does not have the container, it takes it from another node in the background and requests
                    #  the instance from another node as well.
                    try:
                        refund_gas.pop()()  # Refund the gas.
                    except IndexError:
                        pass
                    finally:
                        getting_container = True
                        continue
                except Exception as e:
                    l.LOGGER('Error building the container: ' + str(e))
                    try:
                        refund_gas.pop()()  # Refund the gas.
                    except IndexError:
                        l.LOGGER('Error refunding the gas.')
                    l.LOGGER(str(e))
                    raise e

                # If the request is made by a local service.
                if father_id == father_ip:
                    container = create_container(
                        id=service_id,
                        entrypoint=service.container.entrypoint
                    )

                    set_config(container_id=container.id, config=config.config, resources=initial_system_resources,
                               api=service.container.config)

                    # The container must be started after adding the configuration file and
                    #  before requiring its IP address, since docker assigns it at startup.

                    try:
                        container.start()
                    except docker_lib.errors.APIError as e:
                        l.LOGGER('ERROR ON CONTAINER ' + str(container.id) + ' ' + str(
                            e))  # TODO LOS ERRORES DEBERIAN LANZAR ALGUN TIPO DE EXCEPCION QUE LLEGUE HASTA EL GRPC.

                    # Reload this object from the server again and update attrs with the new data.
                    container.reload()

                    for slot in service.api.slot:
                        uri_slot = celaut.Instance.Uri_Slot()
                        uri_slot.internal_port = slot.port

                        # Since it is internal, we know that it will only have one possible address per slot.
                        uri = celaut.Instance.Uri()
                        uri.ip = container.attrs['NetworkSettings']['IPAddress']
                        uri.port = slot.port
                        uri_slot.uri.append(uri)

                # Si hace la peticion un servicio de otro nodo.
                else:
                    assigment_ports = {slot.port: utils.get_free_port() for slot in service.api.slot}
                    container = create_container(
                        use_other_ports=assigment_ports,
                        id=service_id,
                        entrypoint=service.container.entrypoint
                    )
                    set_config(container_id=container.id, config=config.config, resources=initial_system_resources,
                               api=service.container.config)
                    try:
                        container.start()
                    except docker_lib.errors.APIError as e:
                        # TODO LOS ERRORES DEBERÍAN LANZAR UNA EXCEPCION QUE LLEGUE HASTA EL GRPC.
                        print(service_id, service.container.entrypoint, type(service.container.entrypoint))
                        l.LOGGER('ERROR ON CONTAINER ' + str(container.id) + ' ' + str(e))

                        # Reload this object from the server again and update attrs with the new data.
                    container.reload()

                    for port in assigment_ports:
                        uri_slot = celaut.Instance.Uri_Slot()
                        uri_slot.internal_port = port

                        # for host_ip in host_ip_list:
                        uri = celaut.Instance.Uri()
                        uri.ip = utils.get_local_ip_from_network(
                            network=utils.get_network_name(ip_or_uri=father_ip)
                        )
                        uri.port = assigment_ports[port]
                        uri_slot.uri.append(uri)

                l.LOGGER('Thrown out a new instance by ' + father_id + ' of the container_id ' + container.id)
                return gateway_pb2.Instance(
                    token=add_container(
                        father_id=father_id,
                        container=container,
                        initial_gas_amount=initial_gas_amount,
                        system_requeriments_range=gateway_pb2.ModifyServiceSystemResourcesInput(
                            min_sysreq=initial_system_resources, max_sysreq=initial_system_resources)  # TODO ??
                    ),
                    instance=celaut.Instance(
                        api=service.api,
                        uri_slot=[uri_slot]
                    )
                )

            if abort_it:
                l.LOGGER("Can't launch this service " + service_id)
                raise Exception("Can't launch this service " + service_id)
