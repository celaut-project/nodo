from typing import Optional, Callable, List, Dict

import docker as docker_lib

from protos import celaut_pb2 as celaut, gateway_pb2
from src.gateway.utils import GATEWAY_PORT
from src.virtualizers.docker import build
from src.gateway.launcher.local_execution.create_container import create_container
from src.gateway.launcher.local_execution.set_config import set_config
from src.tunneling_system.tunnels import TunnelSystem
from src.manager.manager import default_initial_cost, add_container
from src.utils import utils, logger as log
from src.utils.env import DEFAULT_SYSTEM_RESOURCES
from src.utils.utils import from_gas_amount
from src.utils.network import get_free_port
from src.virtualizers.docker.firewall import block_all, allow_connection, Protocol


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

    #  TODO check this.
    father_id = father_id if father_id else ""
    father_ip = father_ip if father_ip else ""

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
            log.LOGGER('Error building the container: ' + str(e))
            refund_gas.pop()()  # Refund the gas.
        except IndexError:
            log.LOGGER('Error refunding the gas.')
        finally:
            log.LOGGER(str(e))
            raise e

    # If the request is made by a local service.
    require_tunnel = TunnelSystem().from_tunnel(ip=father_ip)
    by_local: bool = father_id == father_ip and not require_tunnel
    assigment_ports: Optional[Dict[int, int]] = \
        {slot.port: get_free_port() for slot in service.api.slot} if not by_local \
        else {slot.port: slot.port for slot in service.api.slot}

    # TODO START OF virtualizers.docker.execute.py
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
        log.LOGGER('ERROR ON CONTAINER ' + str(container.id) + ' ' + str(e))
        raise e

    # Reload this object from the server again and update attrs with the new data.
    container.reload()

    if not block_all(container_id=container.id):
        log.LOGGER(f"Docker firewall block all function failed for {container.id}")

    if not allow_connection(container_id=container.id, ip='172.17.0.1', port=GATEWAY_PORT, protocol=Protocol.TCP):
        log.LOGGER(f"Docker firewall allow connection function failed for {container.id}")

    # TODO END OF virtualizers.docker.execute.py

    try:
        for internal, external in assigment_ports.items():
            uri_slot = celaut.Instance.Uri_Slot()
            uri_slot.internal_port = internal

            # for host_ip in host_ip_list:
            _ip: str = utils.get_local_ip_from_network(
                network=utils.get_network_name(direction=father_ip)
            ) if not by_local else container.attrs['NetworkSettings']['IPAddress']
            _port: int = external

            log.LOGGER(f"Required tunnel to expose the service {require_tunnel} from ip {father_ip}")
            if require_tunnel:
                _response = TunnelSystem().generate_tunnel(ip=_ip, port=_port)
                if _response:
                    _ip, _port = _response
                else:
                    _msg = "Any tunnel available. Instance can't be serve."
                    log.LOGGER(_msg)
                    # TODO Delete container.
                    raise Exception(_msg)

            log.LOGGER(f"Using uri {_ip}:{_port}")
            uri_slot.uri.append(
                celaut.Instance.Uri(
                    ip=_ip,
                    port=_port
                )
            )
            
    except Exception as e:
        log.LOGGER(f"Exception setting uri_slot: {str(e)}")
        raise e

    instance = celaut.Instance(
            api=service.api,
            uri_slot=[uri_slot]
        )
    
    log.LOGGER(f'Thrown out a new instance by {father_id} of the container_id {container.id}')
    return gateway_pb2.Instance(
        token=add_container(
            father_id=father_id,
            container=container,
            initial_gas_amount=initial_gas_amount,
            serialized_instance=instance.SerializeToString(),
            system_requirements_range=gateway_pb2.ModifyServiceSystemResourcesInput(
                min_sysreq=initial_system_resources, max_sysreq=initial_system_resources)  # TODO ??
            ),
        instance=instance
    )
