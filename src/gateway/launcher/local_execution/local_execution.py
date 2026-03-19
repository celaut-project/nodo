import traceback
from typing import Optional, Callable, List, Dict

from protos import celaut_pb2 as celaut, celaut_pb2
from src.database.sql_connection import SQLConnection
from src.virtualizers.interface import build, execute, get_configured_virtualizer
from src.tunneling_system.tunnels import TunnelSystem
from src.manager.manager import default_initial_cost, provision_vmachine
from src.utils import utils, logger as log
from src.utils.utils import from_gas_amount
from src.utils.network import get_free_port
from src.utils.config import ConfigManager


sc = SQLConnection()
env_manager = ConfigManager()

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
    configured_virtualizer = get_configured_virtualizer()
    log.LOGGER(
        f"Local execution start: service_id={service_id}, father_id={father_id}, "
        f"father_ip={father_ip}, virtualizer={configured_virtualizer}"
    )

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
            log.LOGGER(traceback.format_exc())
            refund_gas.pop()()  # Refund the gas.
        except IndexError:
            log.LOGGER('Error refunding the gas.')
        finally:
            log.LOGGER(str(e))
            raise e
    log.LOGGER(f"Service build ready for execution: service_id={service_id}")

    # If the request is made by a local service (container inside this node).
    require_tunnel = TunnelSystem().from_tunnel(ip=father_ip)
    is_internal_father = bool(father_id) and sc.internal_instance_exists(id=father_id)
    isolate_internal_children = env_manager.get("network.ISOLATE_INTERNAL_CHILDREN", True)
    by_local: bool = is_internal_father and not require_tunnel and isolate_internal_children
    log.LOGGER(
        "Internal child isolation is "
        + ("enabled" if isolate_internal_children else "disabled")
        + f" (father_id={father_id}, father_ip={father_ip}, by_local={by_local})"
    )

    # TODO Control race conditions on get free ports. Maybe using a lock or a port reservation system.
    assigment_ports: Optional[Dict[int, int]] = \
        {slot.port: get_free_port() for slot in service.api.slot} if not by_local \
        else {slot.port: slot.port for slot in service.api.slot}
    log.LOGGER(
        f"Execution network mode: by_local={by_local}, require_tunnel={require_tunnel}, "
        f"assigment_ports={assigment_ports}"
    )

    log.LOGGER(
        f"Invoking virtualizer execute: virtualizer={configured_virtualizer}, "
        f"service_id={service_id}, father_id={father_id}"
    )
    vmachine_id, vmachine_ip = execute(
        assigment_ports=assigment_ports, 
        by_local=by_local, 
        service_id=service_id, 
        service=service, 
        config=config, 
        initial_system_resources=initial_system_resources, 
        father_id=father_id
    )
    log.LOGGER(f"Virtualizer execute returned: vmachine_id={vmachine_id}, vmachine_ip={vmachine_ip}")

    uri_slots: List[celaut.Instance.Uri_Slot] = []
    try:
        resolved_network = utils.get_network_name(direction=father_ip) if not by_local else ""
        log.LOGGER(
            f"Preparing published URI slots: resolved_network={resolved_network if resolved_network else 'by_local'}"
        )
        for internal, external in assigment_ports.items():
            uri_slot = celaut.Instance.Uri_Slot()
            uri_slot.internal_port = internal

            # for host_ip in host_ip_list:
            _ip: str = utils.get_local_ip_from_network(
                    network=resolved_network,
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
            log.LOGGER(
                f"Published URI mapping: internal_port={internal}, advertised={_ip}:{_port}, "
                f"vmachine_ip={vmachine_ip}, by_local={by_local}"
            )
            uri_slots.append(uri_slot)
            
    except Exception as e:
        log.LOGGER(f"Exception setting uri_slot: {str(e)}")
        log.LOGGER(traceback.format_exc())
        raise e

    instance = celaut.Instance(
            api=service.api,
            uri_slot=uri_slots
        )

    provision_vmachine(
        service_id=service_id,
        father_id=father_id,
        vmachine_id=vmachine_id,
        vmachine_ip=vmachine_ip,
        initial_gas_amount=initial_gas_amount,
        serialized_instance=instance.SerializeToString(),
        virtualizer=configured_virtualizer,
        system_requirements_range=celaut_pb2.ModifyServiceSystemResourcesInput(
                min_sysreq=initial_system_resources, 
                max_sysreq=initial_system_resources
            )
        )
    log.LOGGER(
        f"Instance provisioned in DB: vmachine_id={vmachine_id}, virtualizer={configured_virtualizer}, "
        f"uri_slots={len(uri_slots)}"
    )
    
    return celaut_pb2.ServiceInstance(
        token=vmachine_id,
        instance=instance
    )
