from src.virtualizers.docker.build import is_service_built as docker_is_service_built
from src.virtualizers.docker.build import build as docker_build
from src.virtualizers.cloud_hypervisor.build import is_service_built as ch_is_service_built
from src.virtualizers.cloud_hypervisor.build import build as ch_build
from src.virtualizers.cloud_hypervisor.execute import execute as ch_execute
from src.virtualizers.docker.execute import execute as docker_execute
from src.virtualizers.docker.remove import remove as remove_docker
from src.virtualizers.docker.hotplug import hotplug as docker_hotplug
from src.virtualizers.docker.kill import kill as docker_kill
from src.virtualizers.docker.firewall import remove_rule as docker_remove_rule
from src.virtualizers.docker.maintain import maintain as docker_maintain
from src.virtualizers.firewall import TransportProtocol
from typing import Optional, Callable, Dict, Tuple
from src.virtualizers.architecture import check_supported_architecture, UnsupportedArchitectureException
from protos import celaut_pb2
from src.utils.logger import LOGGER as l
from src.utils.config import ConfigManager
from src.database.sql_connection import SQLConnection


"""
This interface defines the functions that any virtualizer implementation must provide.
Currently, it is implemented by the Docker virtualizer, but it can be extended to support other virtualization technologies in the future.
"""

env_manager = ConfigManager()
sc = SQLConnection()

def _get_default_virtualizer() -> str:
    return env_manager.get("virtualizers.DEFAULT_VIRTUALIZER", "docker")

def _resolve_virtualizer_for_instance(vmachine_id: str) -> str:
    try:
        v = sc.get_internal_virtualizer(id=vmachine_id)
        if v:
            v = str(v).strip()
            if v:
                return v
    except Exception as e:
        l.LOGGER(f"Error reading virtualizer for {vmachine_id}: {e}")
    return _get_default_virtualizer()

def _is_supported_virtualizer(name: str) -> bool:
    return name in {"docker", "cloud_hypervisor"}

def _ensure_usable_virtualizer(name: str, allow_cloud_hypervisor: bool = False) -> str:
    if not _is_supported_virtualizer(name):
        l.LOGGER(f"Unknown virtualizer '{name}', falling back to docker.")
        return "docker"
    if name == "cloud_hypervisor" and not allow_cloud_hypervisor:
        raise NotImplementedError(
            "Virtualizer 'cloud_hypervisor' is configured but not implemented yet."
        )
    return name

def get_configured_virtualizer() -> str:
    return _ensure_usable_virtualizer(
        _get_default_virtualizer(),
        allow_cloud_hypervisor=True,
    )

def is_built(service_hash: str) -> bool:
    """Check if a service with the given hash is already built."""
    virtualizer = _ensure_usable_virtualizer(
        _get_default_virtualizer(),
        allow_cloud_hypervisor=True,
    )
    if virtualizer == "cloud_hypervisor":
        return ch_is_service_built(service_hash)
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

    virtualizer = _ensure_usable_virtualizer(
        _get_default_virtualizer(),
        allow_cloud_hypervisor=True,
    )

    if virtualizer == "cloud_hypervisor":
        return ch_build(
            service=service,
            metadata=metadata,
            service_id=service_id,
        )

    return docker_build(
        service=service,
        metadata=metadata,
        service_id=service_id,
    )

def hotplug(
        vmachine_id: str,
        system_requeriments_range: celaut_pb2.ModifyServiceSystemResourcesInput
) -> bool:
    """Modify the system requirements of a running service."""
    _ensure_usable_virtualizer(_resolve_virtualizer_for_instance(vmachine_id))
    return docker_hotplug(
        container_id=vmachine_id,
        system_requeriments_range=system_requeriments_range
    )

def kill(vmachine_id: str) -> bool:
    """Kill a running service."""
    _ensure_usable_virtualizer(_resolve_virtualizer_for_instance(vmachine_id))
    return docker_kill(vmachine_id=vmachine_id)

def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    """Check the status of a running service and remove it if it has exited."""
    _ensure_usable_virtualizer(_resolve_virtualizer_for_instance(vmachine_id))
    docker_maintain(
        vmachine_id=vmachine_id,
        debug_mode=debug_mode,
        remove_and_penalize=remove_and_penalize
    )

def execute(
        assigment_ports: Optional[Dict[int, int]],
        by_local: bool,
        service_id: str,
        service: celaut_pb2.Service,
        config: Optional[celaut_pb2.Configuration],
        initial_system_resources: celaut_pb2.Sysresources,
        father_id: str,
) -> Tuple[str, str]:
    """
    Execute a built service and return (vmachine_id, vmachine_ip).

    Note: This is currently backed by the Docker virtualizer. It exists to allow
    the rest of the codebase to stop importing Docker-specific implementations
    directly and make it possible to add other virtualizers (e.g. Cloud Hypervisor)
    behind this interface.
    """
    virtualizer = _ensure_usable_virtualizer(
        _get_default_virtualizer(),
        allow_cloud_hypervisor=True,
    )
    if virtualizer == "cloud_hypervisor":
        return ch_execute(
            assigment_ports=assigment_ports,
            by_local=by_local,
            service_id=service_id,
            service=service,
            config=config,
            initial_system_resources=initial_system_resources,
            father_id=father_id,
        )

    return docker_execute(
        assigment_ports=assigment_ports,
        by_local=by_local,
        service_id=service_id,
        service=service,
        config=config,
        initial_system_resources=initial_system_resources,
        father_id=father_id,
    )

def remove(vmachine_id: str) -> bool:
    """Remove a service."""
    _ensure_usable_virtualizer(_resolve_virtualizer_for_instance(vmachine_id))
    return remove_docker(vmachine_id=vmachine_id)

def remove_firewall_rule(
        vmachine_id: str,
        ip: str,
        port: int,
        protocol: TransportProtocol
) -> bool:
    """Remove a firewall rule for a given container."""
    _ensure_usable_virtualizer(_resolve_virtualizer_for_instance(vmachine_id))
    return docker_remove_rule(
        container_id=vmachine_id,
        ip=ip,
        port=port,
        protocol=protocol
    )
