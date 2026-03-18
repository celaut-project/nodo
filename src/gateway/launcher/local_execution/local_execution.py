from typing import Optional, Callable, List, Dict

from protos import celaut_pb2 as celaut, celaut_pb2
from src.database.sql_connection import SQLConnection
from src.virtualizers.interface import build, execute
from src.tunneling_system.tunnels import TunnelSystem
from src.manager.manager import default_initial_cost, provision_vmachine
from src.utils import utils, logger as log
from src.utils.utils import from_gas_amount
from src.utils.network import get_free_port


sc = SQLConnection()

def local_execution(
        config: Optional[celaut_pb2.Configuration],
        resources: celaut_pb2.Service.Container.Resources,
        father_id: Optional[str],
        father_ip: Optional[str],
        metadata: celaut.Metadata,
        service: celaut.Service,
        service_id: Optional[str],
        refund_gas: List[Callable]
) -> celaut_pb2.ServiceInstance:

    #  TODO check this.
    father_id = father_id if father_id else ""
    father_ip = father_ip if father_ip else ""

    initial_gas_amount: int = from_gas_amount(config.initial_gas_amount) \
        if config.HasField("initial_gas_amount") else default_initial_cost(father_id=father_id)

    initial_system_resources: celaut.Sysresources = resources.at_init

    try:
        service_id = build(
            service=service,
            metadata=metadata,
            service_id=service_id,
        )  # If the service is not built, build it.
    except Exception as e:
        try:
            log.LOGGER('Error building the service: ' + str(e))
            refund_gas.pop()()  # Refund the gas.
        except IndexError:
            log.LOGGER('Error refunding the gas.')
        finally:
            log.LOGGER(str(e))
            raise e

    # If the request is made by a local service (container inside this node).
    require_tunnel = TunnelSystem().from_tunnel(ip=father_ip)
    is_internal_father = bool(father_id) and sc.internal_instance_exists(id=father_id)
    by_local: bool = is_internal_father and not require_tunnel
    assigment_ports: Optional[Dict[int, int]] = \
        {slot.port: get_free_port() for slot in service.api.slot} if not by_local \
        else {slot.port: slot.port for slot in service.api.slot}

    vmachine_id, vmachine_ip = execute(
        assigment_ports=assigment_ports, 
        by_local=by_local, 
        service_id=service_id, 
        service=service, 
        config=config, 
        initial_system_resources=initial_system_resources, 
        father_id=father_id
    )

    try:
        for internal, external in assigment_ports.items():
            uri_slot = celaut.Instance.Uri_Slot()
            uri_slot.internal_port = internal

            # for host_ip in host_ip_list:
            _ip: str = utils.get_local_ip_from_network(
                    network=utils.get_network_name(direction=father_ip),
                ) if not by_local else vmachine_ip
            _port: int = external

            if require_tunnel:
                _response = TunnelSystem().generate_tunnel(ip=_ip, port=_port)
                if _response:
                    _ip, _port = _response
                else:
                    _msg = "Any tunnel available. Instance can't be serve."
                    log.LOGGER(_msg)
                    # TODO Delete service using virtualizers.interfaze.kill.
                    raise Exception(_msg)

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

    provision_vmachine(
        service_id=service_id,
        father_id=father_id,
        vmachine_id=vmachine_id,
        vmachine_ip=vmachine_ip,
        initial_gas_amount=initial_gas_amount,
        serialized_instance=instance.SerializeToString(),
        system_requirements_range=celaut_pb2.ModifyServiceSystemResourcesInput(
                min_sysreq=initial_system_resources, 
                max_sysreq=initial_system_resources
            )
        )
    
    return celaut_pb2.ServiceInstance(
        token=vmachine_id,
        instance=instance
    )
