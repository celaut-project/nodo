from src.virtualizers.docker.build import is_service_built as docker_is_service_built
from src.virtualizers.docker.build import build as docker_build
from src.virtualizers.docker.hotplug import hotplug as docker_hotplug
from src.virtualizers.docker.kill import kill as docker_kill
from src.virtualizers.docker.firewall import remove_rule as docker_remove_rule
from src.virtualizers.architecture import check_supported_architecture, UnsupportedArchitectureException
from protos import celaut_pb2
from src.utils.logger import LOGGER as l

def is_service_build(service_hash: str) -> bool:
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

def remove_firewall_rule(
        vmachine_id: str,
        ip: str,
        port: int,
        protocol: Protocol
) -> bool:
    """Remove a firewall rule for a given container."""
    return docker_remove_rule(
        container_id=vmachine_id,
        ip=ip,
        port=port,
        protocol=protocol
    )