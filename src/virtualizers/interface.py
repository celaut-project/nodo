from src.virtualizers.docker.build import is_service_built as docker_is_service_built
from src.virtualizers.docker.build import build as docker_build
from src.virtualizers.docker.hotplug import hotplug as docker_hotplug
from src.virtualizers.docker.kill import kill as docker_kill
from src.virtualizers.docker.firewall import remove_rule as docker_remove_rule
from src.virtualizers.docker.maintain import maintain as docker_maintain
from src.virtualizers.docker.firewall import TransportProtocol
from typing import Optional, Callable
from src.virtualizers.architecture import check_supported_architecture, UnsupportedArchitectureException
from protos import celaut_pb2
from src.utils.logger import LOGGER as l


"""
This interface defines the functions that any virtualizer implementation must provide.
Currently, it is implemented by the Docker virtualizer, but it can be extended to support other virtualization technologies in the future.
"""

def is_built(service_hash: str) -> bool:
    """Check if a service with the given hash is already built."""
    return docker_is_service_built(service_hash)

def build(
        service: celaut_pb2.Service,
        metadata: celaut_pb2.Metadata,
        service_id: Optional[str] = None,
) -> str:
    """Build a service and return its identifier."""
    if not check_supported_architecture(service=service, metadata=metadata):
        l.LOGGER('Build process of ' + service_id + ': unsupported architecture.')
        raise UnsupportedArchitectureException(arch=str(metadata))

    return docker_build(
        service=service,
        metadata=metadata,
        service_id=service_id
    )

def hotplug(
        vmachine_id: str,
        system_requeriments_range: celaut_pb2.ModifyServiceSystemResourcesInput
) -> bool:
    """Modify the system requirements of a running service."""
    return docker_hotplug(
        container_id=vmachine_id,
        system_requeriments_range=system_requeriments_range
    )

def kill(vmachine_id: str) -> bool:
    """Kill a running service."""
    return docker_kill(vmachine_id=vmachine_id)

def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    """Check the status of a running service and remove it if it has exited."""
    docker_maintain(
        vmachine_id=vmachine_id,
        debug_mode=debug_mode,
        remove_and_penalize=remove_and_penalize
    )

def remove_firewall_rule(
        vmachine_id: str,
        ip: str,
        port: int,
        protocol: TransportProtocol
) -> bool:
    """Remove a firewall rule for a given container."""
    return docker_remove_rule(
        container_id=vmachine_id,
        ip=ip,
        port=port,
        protocol=protocol
    )
