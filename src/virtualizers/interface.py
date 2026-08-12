from src.virtualizers.ch.build import is_service_built as ch_is_service_built
from src.virtualizers.ch.build import build as ch_build
from src.virtualizers.ch.execute import execute as ch_execute
from src.virtualizers.ch.hotplug import hotplug as ch_hotplug
from src.virtualizers.ch.kill import kill as ch_kill
from src.virtualizers.ch.maintain import maintain as ch_maintain
from src.virtualizers.firewall import TransportProtocol, remove_rule as vm_remove_rule
from typing import Optional, Callable, Dict, Tuple
from src.virtualizers.architecture import check_supported_architecture, UnsupportedArchitectureException
from protos import celaut_pb2
from src.utils.logger import LOGGER as log
from src.utils.config import ConfigManager
from src.database.sql_connection import SQLConnection


"""
This interface defines the functions that any virtualizer implementation must provide.

Cloud Hypervisor (CH) is the only supported virtualizer. The Docker virtualizer
was removed so the node no longer depends on a local Docker install; service
packing is delegated to the external packer-service.
"""

env_manager = ConfigManager()
sc = SQLConnection()

def get_configured_virtualizer() -> str:
    return "ch"

def is_built(service_hash: str) -> bool:
    """Check if a service with the given hash is already built."""
    return ch_is_service_built(service_hash)

def build(
        service: celaut_pb2.Service,
        metadata: celaut_pb2.Metadata,
        service_id: Optional[str] = None,
) -> str:
    """Build a service and return its identifier."""
    if not check_supported_architecture(service=service, metadata=metadata):
        log(f'Build process of {service_id}: unsupported architecture.')
        raise UnsupportedArchitectureException(arch=str(metadata))

    return ch_build(
        service=service,
        metadata=metadata,
        service_id=service_id,
    )

def hotplug(
        vmachine_id: str,
        system_requeriments_range: celaut_pb2.ModifyServiceSystemResourcesInput
) -> bool:
    """Modify the system requirements of a running service."""
    return ch_hotplug(
        vmachine_id=vmachine_id,
        system_requeriments_range=system_requeriments_range,
    )

def kill(vmachine_id: str) -> bool:
    """Kill a running service."""
    return ch_kill(vmachine_id=vmachine_id)

def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    """Check the status of a running service and remove it if it has exited."""
    ch_maintain(
        vmachine_id=vmachine_id,
        debug_mode=debug_mode,
        remove_and_penalize=remove_and_penalize,
    )

def execute(
        assigment_ports: Optional[Dict[int, int]],
        by_local: bool,
        service_id: str,
        service: celaut_pb2.Service,
        config: Optional[celaut_pb2.Configuration],
        initial_system_resources: celaut_pb2.Sysresources,
        father_id: str,
) -> Tuple[str, str, celaut_pb2.Sysresources]:
    """
    Execute a built service and return (vmachine_id, vmachine_ip, resolved_resources).

    ``resolved_resources`` is what the virtualizer actually reserved for the guest --
    defaults and floors already applied -- so the launcher persists what the instance
    holds rather than what its manifest requested (#249).
    """
    return ch_execute(
        assigment_ports=assigment_ports,
        by_local=by_local,
        service_id=service_id,
        service=service,
        config=config,
        initial_system_resources=initial_system_resources,
        father_id=father_id,
    )

def remove_firewall_rule(
        vmachine_id: str,
        ip: str,
        port: int,
        protocol: TransportProtocol
) -> bool:
    """Remove a firewall rule for a given container."""
    return vm_remove_rule(
        vmachine_id=vmachine_id,
        ip=ip,
        port=port,
        protocol=protocol
    )
