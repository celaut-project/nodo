import json
import traceback
from typing import Optional, Callable, List, Dict

import netifaces as ni

from protos import celaut_pb2 as celaut, celaut_pb2
from src.database.sql_connection import SQLConnection
from src.virtualizers.interface import build, execute, get_configured_virtualizer
from src.manager.manager import (
    default_initial_cost,
    is_external_execute_client,
    reserve_instance_name,
)
from src.utils import utils, logger as log
from src.utils.instance_names import extract_instance_name
from src.utils.utils import from_gas_amount
from src.utils.network import get_free_port
from src.utils.config import ConfigManager
from src.virtualizers.firewall import resolve_slot_transport_protocols


sc = SQLConnection()
env_manager = ConfigManager()


def _serialize_envs(config: Optional[celaut.Configuration]) -> str:
    """Serialize a Configuration's ``environment_variables`` map to JSON text.

    The instance is launched with these envs (see ``execute()``), so persisting them
    lets the node later tell how an instance was configured — e.g. whether a
    source-application was started as a seed signer (``SOURCE_SIGNER_MODE=seed``).
    Values are protobuf ``bytes``; they are decoded as UTF-8 (env vars are text).
    Returns ``None`` when there are no env vars, so the DB column stays NULL.
    """
    if config is None or not config.environment_variables:
        return None
    envs = {
        key: value.decode("utf-8", errors="replace")
        for key, value in config.environment_variables.items()
    }
    return json.dumps(envs, sort_keys=True) if envs else ""


_INTERFACE_PREFIX_PRIORITY = (
    "wl",
    "ww",
    "en",
    "eth",
)


def _interface_priority(interface: str) -> tuple[int, int, str]:
    normalized = (interface or "").strip().lower()
    if normalized in {"lo", "localhost"}:
        return (3, len(normalized), normalized)
    if utils.is_virtual_interface(normalized):
        return (2, len(normalized), normalized)
    if normalized.startswith(_INTERFACE_PREFIX_PRIORITY):
        return (0, len(normalized), normalized)
    return (1, len(normalized), normalized)


def _resolve_default_ipv4_interface() -> str:
    try:
        default_gateway = ni.gateways().get("default", {})
        default_route = default_gateway.get(ni.AF_INET)
        if default_route and len(default_route) > 1:
            return str(default_route[1])
    except Exception as e:
        log.LOGGER(f"Unable to resolve default IPv4 interface: {e}")
    return ""


def _resolve_default_ipv6_interface() -> str:
    try:
        default_gateway = ni.gateways().get("default", {})
        default_route = default_gateway.get(ni.AF_INET6)
        if default_route and len(default_route) > 1:
            return str(default_route[1])
    except Exception as e:
        log.LOGGER(f"Unable to resolve default IPv6 interface: {e}")
    return ""


def _find_any_host_interface_ip() -> str:
    for interface in sorted(ni.interfaces(), key=_interface_priority):
        if _interface_priority(interface)[0] >= 2:
            continue
        if interface in {"lo", "localhost"}:
            continue
        try:
            return utils.get_local_ip_from_network(network=interface, allow_link_local=False)
        except Exception:
            continue
    raise RuntimeError("Unable to find any host interface IP to advertise.")


def _get_external_advertised_host_ip(father_ip: str) -> str:
    configured_public_ip = str(env_manager.get("network.PUBLIC_IP", "") or "").strip()
    if configured_public_ip:
        return configured_public_ip

    configured_interface = str(env_manager.get("network.EXTERNAL_INTERFACE", "") or "").strip()
    if configured_interface:
        return utils.get_local_ip_from_network(network=configured_interface, allow_link_local=False)

    default_interface = _resolve_default_ipv4_interface()
    if default_interface:
        return utils.get_local_ip_from_network(network=default_interface, allow_link_local=False)

    default_ipv6_interface = _resolve_default_ipv6_interface()
    if default_ipv6_interface:
        return utils.get_local_ip_from_network(network=default_ipv6_interface, allow_link_local=False)

    try:
        return _find_any_host_interface_ip()
    except Exception as e:
        log.LOGGER(f"Unable to resolve host IP from available interfaces: {e}")

    if father_ip:
        resolved_network = utils.get_network_name(direction=father_ip)
        if resolved_network:
            return utils.get_local_ip_from_network(network=resolved_network, allow_link_local=False)

    raise RuntimeError(
        "Unable to resolve an external host IP to advertise. "
        "Configure network.PUBLIC_IP or network.EXTERNAL_INTERFACE."
    )

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
    requested_instance_name, sanitized_config = extract_instance_name(config)
    config = sanitized_config or celaut_pb2.Configuration()
    instance_name = reserve_instance_name(requested_name=requested_instance_name)
    configured_virtualizer = get_configured_virtualizer()
    log.LOGGER(
        f"Local execution start: service_id={service_id}, father_id={father_id}, "
        f"father_ip={father_ip}, virtualizer={configured_virtualizer}, instance_name={instance_name}"
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
    force_external_exposure = is_external_execute_client(father_id)
    # In case of dev instances, we consider them as internal.
    # If the father is internal, but isolate internal children is disabled, the child should be exposed outside.
    expose_outside: bool = not disabled_outside and (
        force_external_exposure
        or (not is_dev_client and (not father_is_local_vmachine or not isolate_internal_children))
    )
    if force_external_exposure and disabled_outside:
        log.LOGGER(
            "External exposure requested by configuration, but network.DISABLE_EXPOSE_OUTSIDE is enabled."
        )
    log.LOGGER(
        "Internal child isolation is "
        + ("enabled" if isolate_internal_children else "disabled")
        + (
            f" (father_id={father_id}, father_ip={father_ip}, by_local={not expose_outside}, "
            f"force_external_exposure={force_external_exposure})"
        )
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
    assigment_ports = {
        port: (
            get_free_port(free_port_ranges=free_port_ranges)
            if expose_outside
            else port
        )
        for port in supported_slot_ports
    }
    log.LOGGER(
        f"Execution network mode: by_local={not expose_outside}, "
        f"assigment_ports={assigment_ports}"
    )

    log.LOGGER(
        f"Invoking virtualizer execute: virtualizer={configured_virtualizer}, "
        f"service_id={service_id}, father_id={father_id}"
    )

    # Execute virtualizer process.
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

    # Resolve slots
    uri_slots: List[celaut.Instance.Uri_Slot] = []
    resolved_network = ""
    try:
        if expose_outside and not force_external_exposure:
            resolved_network = utils.get_network_name(direction=father_ip)

        log.LOGGER(f"Preparing published URI slots: resolved_network={resolved_network} and father IP={father_ip if father_ip else 'N/A'}")

        # get the host ip to be published for this instance. If the instance doesn't require to be exposed, publish the vmachine_ip, otherwise publish the local IP of this node.:
        if not expose_outside:
            _ip = vmachine_ip
        elif force_external_exposure:
            _ip = _get_external_advertised_host_ip(father_ip=father_ip)
        else:
            _ip = utils.get_local_ip_from_network(
                network=resolved_network,
                allow_link_local=False,
            )

        for internal, external in assigment_ports.items():
            uri_slot = celaut.Instance.Uri_Slot()
            uri_slot.internal_port = internal

            uri_slot.uri.append(
                celaut.Instance.Uri(
                    ip=_ip,
                    port=external
                )
            )
            log.LOGGER(
                f"Published URI mapping: internal_port={internal}, advertised={_ip}:{external}, "
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

    # Store the instance in the database, including the disk space from the system requirements range.
    system_requirements_range=celaut_pb2.ModifyServiceSystemResourcesInput(
                min_sysreq=initial_system_resources,
                max_sysreq=initial_system_resources
            )
    disk_space = None
    if system_requirements_range and system_requirements_range.max_sysreq:
        sysreq = system_requirements_range.max_sysreq
        if sysreq.HasField("disk_space"):
            disk_space = int(sysreq.disk_space)
    if not disk_space:
        raise Exception("Disk space is not specified in the system requirements range.")

    sc.add_local_instance(
        father_id=father_id,
        container_id=vmachine_id,
        name=instance_name,
        container_ip=vmachine_ip,
        gas=initial_gas_amount,
        serialized_instance=instance.SerializeToString(),
        service_id=service_id,
        virtualizer=configured_virtualizer,
        disk_space=disk_space,
        envs=_serialize_envs(config),
    )
    log.LOGGER(
        f"Instance provisioned in DB: vmachine_id={vmachine_id}, virtualizer={configured_virtualizer}, "
        f"uri_slots={len(uri_slots)}"
    )
    
    return celaut_pb2.ServiceInstance(
        token=vmachine_id,
        instance=instance
    )
