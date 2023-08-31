from typing import Optional, Callable, List, Dict

import docker as docker_lib

from protos import celaut_pb2 as celaut, gateway_pb2
from src.builder import build
from src.gateway.launcher.local_execution.create_container import create_container
from src.gateway.launcher.local_execution.set_config import set_config
from src.manager.manager import default_initial_cost, add_container
from src.utils import utils, logger as l
from src.utils.env import DEFAULT_SYSTEM_RESOURCES
from src.utils.utils import from_gas_amount


def local_execution(
        config: Optional[gateway_pb2.Configuration],
        resources: gateway_pb2.CombinationResources.Clause,
        father_id: Optional[str],
        father_ip: Optional[str],
        metadata: celaut.Any.Metadata,
        service: celaut.Service,
        service_id: Optional[str],
        refund_gas: List[Callable]
) -> gateway_pb2.Instance:

    initial_gas_amount: int = from_gas_amount(config.initial_gas_amount) \
        if config.HasField("initial_gas_amount") else default_initial_cost(father_id=father_id)

    initial_system_resources: celaut.Sysresources = resources.min_sysreq \
        if resources.HasField('min_sysreq') and resources.min_sysreq else DEFAULT_SYSTEM_RESOURCES

    try:
        service_id = build.build(
            service=service,
            metadata=metadata,
            service_id=service_id,
        )  # If the container is not built, build it.
    except Exception as e:
        try:
            l.LOGGER('Error building the container: ' + str(e))
            refund_gas.pop()()  # Refund the gas.
        except IndexError:
            l.LOGGER('Error refunding the gas.')
        finally:
            l.LOGGER(str(e))
            raise e

    # If the request is made by a local service.
    by_local: bool = father_id == father_ip
    assigment_ports: Optional[Dict[int, int]] = {slot.port: utils.get_free_port() for slot in service.api.slot} \
        if not by_local else {slot.port: slot.port for slot in service.api.slot}

    container = create_container(
        use_other_ports=assigment_ports if not by_local else None,
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
        l.LOGGER('ERROR ON CONTAINER ' + str(container.id) + ' ' + str(e))

    # Reload this object from the server again and update attrs with the new data.
    container.reload()

    for internal, external in assigment_ports:
        uri_slot = celaut.Instance.Uri_Slot()
        uri_slot.internal_port = internal

        # for host_ip in host_ip_list:
        uri = celaut.Instance.Uri()
        uri.ip = utils.get_local_ip_from_network(
            network=utils.get_network_name(ip_or_uri=father_ip)
        ) if not by_local else container.attrs['NetworkSettings']['IPAddress']
        uri.port = external
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
