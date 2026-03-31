import traceback
from typing import Optional, Callable, List, Dict

from protos import celaut_pb2 as celaut, celaut_pb2
from src.database.sql_connection import SQLConnection
from src.virtualizers.interface import build, execute, get_configured_virtualizer
from src.manager.manager import default_initial_cost, provision_vmachine
from src.utils import utils, logger as log
from src.utils.utils import from_gas_amount
from src.utils.network import get_free_port
from src.utils.config import ConfigManager
from src.virtualizers.firewall import resolve_slot_transport_protocols


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

    father_is_local_vmachine = bool(father_id) and sc.internal_instance_exists(id=father_id)
    isolate_internal_children = env_manager.get("network.ISOLATE_INTERNAL_CHILDREN", True)
    is_dev_client = "dev" in father_id and env_manager.get("network.CONSIDER_DEV_AS_INTERNAL", True)
    disabled_outside = env_manager.get("network.DISABLE_EXPOSE_OUTSIDE", False)
    # In case of dev instances, we consider them as internal.
    # If the father is internal, but isolate internal children is disabled, the child should be exposed outside.
    expose_outside: bool = not disabled_outside and not is_dev_client and (not father_is_local_vmachine or not isolate_internal_children)
    log.LOGGER(
        "Internal child isolation is "
        + ("enabled" if isolate_internal_children else "disabled")
        + f" (father_id={father_id}, father_ip={father_ip}, by_local={not expose_outside})"
    )

    supported_slot_ports: List[int] = []
    for slot in service.api.slot:
        protocol = resolve_slot_transport_protocols(
            slot,
            logger_fn=log.LOGGER,
            context="[LOCAL_EXEC]",
        )
        if not protocol:
            log.LOGGER(
                f"[LOCAL_EXEC] Slot port={slot.port} ignored because it has no host-supported transports."
            )
            continue
        supported_slot_ports.append(slot.port)

    if not supported_slot_ports:
        log.LOGGER(
            "[LOCAL_EXEC] No host-supported API slots found. Service will be started without published URI slots."
        )

    free_port_ranges = env_manager.get("network.FREE_PORTS_RANGE", [])
    assigment_ports: Optional[Dict[int, int]] = (
        {port: get_free_port(free_port_ranges=free_port_ranges) for port in supported_slot_ports}
        if expose_outside
        else {port: port for port in supported_slot_ports}
    )
    log.LOGGER(
        f"Execution network mode: by_local={not expose_outside}, "
        f"assigment_ports={assigment_ports}"
    )

    log.LOGGER(
        f"Invoking virtualizer execute: virtualizer={configured_virtualizer}, "
        f"service_id={service_id}, father_id={father_id}"
    )
    vmachine_id, vmachine_ip = execute(
        assigment_ports=assigment_ports, 
        by_local=not expose_outside, 
        service_id=service_id, 
        service=service, 
        config=config, 
        initial_system_resources=initial_system_resources, 
        father_id=father_id
    )
    log.LOGGER(f"Virtualizer execute returned: vmachine_id={vmachine_id}, vmachine_ip={vmachine_ip}")

    uri_slots: List[celaut.Instance.Uri_Slot] = []
    resolved_network = ""
    try:
        if expose_outside: 
            resolved_network = utils.get_network_name(direction=father_ip)
        log.LOGGER(f"Preparing published URI slots: resolved_network={resolved_network} and father IP={father_ip if father_ip else 'N/A'}")

        for internal, external in assigment_ports.items():
            uri_slot = celaut.Instance.Uri_Slot()
            uri_slot.internal_port = internal

            # get the host ip to be published for this instance. If the instance doesn't require to be exposed, publish the vmachine_ip, otherwise publish the local IP of this node.:
            _ip: str = utils.get_local_ip_from_network(
                    network=resolved_network,
                ) if expose_outside else vmachine_ip
            _port: int = external

            uri_slot.uri.append(
                celaut.Instance.Uri(
                    ip=_ip,
                    port=_port
                )
            )
            log.LOGGER(
                f"Published URI mapping: internal_port={internal}, advertised={_ip}:{_port}, "
                f"vmachine_ip={vmachine_ip}, by_local={not expose_outside}"
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
