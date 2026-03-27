from src.virtualizers.docker.build import is_service_built as docker_is_service_built
from src.virtualizers.docker.build import build as docker_build
from src.virtualizers.ch.build import is_service_built as ch_is_service_built
from src.virtualizers.ch.build import build as ch_build
from src.virtualizers.ch.execute import execute as ch_execute
from src.virtualizers.ch.hotplug import hotplug as ch_hotplug
from src.virtualizers.ch.kill import kill as ch_kill
from src.virtualizers.ch.maintain import maintain as ch_maintain
from src.virtualizers.ch.remove import remove as remove_ch
from src.virtualizers.docker.execute import execute as docker_execute
from src.virtualizers.docker.remove import remove as remove_docker
from src.virtualizers.docker.hotplug import hotplug as docker_hotplug
from src.virtualizers.docker.kill import kill as docker_kill
from src.virtualizers.docker.maintain import maintain as docker_maintain
from src.virtualizers.firewall import TransportProtocol, remove_rule as vm_remove_rule
from typing import Optional, Callable, Dict, Tuple
from src.virtualizers.architecture import check_supported_architecture, UnsupportedArchitectureException
from protos import celaut_pb2
from src.utils.logger import LOGGER as log
from src.utils.config import ConfigManager
from src.database.sql_connection import SQLConnection


"""
This interface defines the functions that any virtualizer implementation must provide.
Currently, it is implemented by the Docker and Cloud Hypervisor virtualizers.
"""

env_manager = ConfigManager()
sc = SQLConnection()

def _get_default_virtualizer() -> str:
    return env_manager.get("virtualizers.DEFAULT_VIRTUALIZER", "ch")

def _resolve_virtualizer_for_instance(vmachine_id: str) -> str:
    try:
        v = sc.get_internal_virtualizer(id=vmachine_id)
        if v:
            v = str(v).strip()
            if v:
                return v
    except Exception as e:
        log(f"Error reading virtualizer for {vmachine_id}: {e}")
    return _get_default_virtualizer()

def _is_supported_virtualizer(name: str) -> bool:
    return name in {"docker", "ch"}

def _ensure_usable_virtualizer(name: str) -> str:
    if not _is_supported_virtualizer(name):
        raise ValueError(f"Unknown or unsupported virtualizer '{name}'. Supported: docker, ch.")
    return name

def get_configured_virtualizer() -> str:
    return _ensure_usable_virtualizer(
        _get_default_virtualizer(),
    )

def is_built(service_hash: str) -> bool:
    """Check if a service with the given hash is already built."""
    virtualizer = _ensure_usable_virtualizer(
        _get_default_virtualizer(),
    )
    if virtualizer == "ch":
        return ch_is_service_built(service_hash)
    if virtualizer == "docker":
        return docker_is_service_built(service_hash)
    raise ValueError(f"Unknown virtualizer for checking if service {service_hash} is built")

def build(
        service: celaut_pb2.Service,
        metadata: celaut_pb2.Metadata,
        service_id: Optional[str] = None,
) -> str:
    """Build a service and return its identifier."""
    if not check_supported_architecture(service=service, metadata=metadata):
        log(f'Build process of {service_id}: unsupported architecture.')
        raise UnsupportedArchitectureException(arch=str(metadata))

    virtualizer = _ensure_usable_virtualizer(
        _get_default_virtualizer(),
    )

    if virtualizer == "ch":
        return ch_build(
            service=service,
            metadata=metadata,
            service_id=service_id,
        )

    if virtualizer == "docker":
        return docker_build(
            service=service,
            metadata=metadata,
            service_id=service_id,
        )
    raise ValueError(f"Unknown virtualizer for building service {service_id}")

def hotplug(
        vmachine_id: str,
        system_requeriments_range: celaut_pb2.ModifyServiceSystemResourcesInput
) -> bool:
    """Modify the system requirements of a running service."""
    virtualizer = _ensure_usable_virtualizer(
        _resolve_virtualizer_for_instance(vmachine_id),
    )
    if virtualizer == "ch":
        return ch_hotplug(
            vmachine_id=vmachine_id,
            system_requeriments_range=system_requeriments_range,
        )
    if virtualizer == "docker":
        return docker_hotplug(
            container_id=vmachine_id,
            system_requeriments_range=system_requeriments_range
        )
    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")

def kill(vmachine_id: str) -> bool:
    """Kill a running service."""
    virtualizer = _ensure_usable_virtualizer(
        _resolve_virtualizer_for_instance(vmachine_id),
    )
    if virtualizer == "ch":
        return ch_kill(vmachine_id=vmachine_id)
    if virtualizer == "docker":
        return docker_kill(vmachine_id=vmachine_id)
    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")

def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    """Check the status of a running service and remove it if it has exited."""
    virtualizer = _ensure_usable_virtualizer(
        _resolve_virtualizer_for_instance(vmachine_id),
    )
    if virtualizer == "ch":
        ch_maintain(
            vmachine_id=vmachine_id,
            debug_mode=debug_mode,
            remove_and_penalize=remove_and_penalize,
        )
        return
    if virtualizer == "docker":
        docker_maintain(
            vmachine_id=vmachine_id,
            debug_mode=debug_mode,
            remove_and_penalize=remove_and_penalize
        )
    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")

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
    """
    virtualizer = _ensure_usable_virtualizer(
        _get_default_virtualizer(),
    )
    if virtualizer == "ch":
        return ch_execute(
            assigment_ports=assigment_ports,
            by_local=by_local,
            service_id=service_id,
            service=service,
            config=config,
            initial_system_resources=initial_system_resources,
            father_id=father_id,
        )

    if virtualizer == "docker":
        return docker_execute(
            assigment_ports=assigment_ports,
            by_local=by_local,
            service_id=service_id,
            service=service,
            config=config,
            initial_system_resources=initial_system_resources,
            father_id=father_id,
        )
    raise ValueError(f"Unknown virtualizer for executing service {service_id}")

def remove(vmachine_id: str) -> bool:
    """Remove a service."""
    virtualizer = _ensure_usable_virtualizer(
        _resolve_virtualizer_for_instance(vmachine_id),
    )
    if virtualizer == "ch":
        return remove_ch(vmachine_id=vmachine_id)
    if virtualizer == "docker":
        return remove_docker(vmachine_id=vmachine_id)
    raise ValueError(f"Unknown virtualizer for instance {vmachine_id}")

def remove_firewall_rule(
        vmachine_id: str,
        ip: str,
        port: int,
        protocol: TransportProtocol
) -> bool:
    """Remove a firewall rule for a given container."""
    _ensure_usable_virtualizer(
        _resolve_virtualizer_for_instance(vmachine_id),
    )
    return vm_remove_rule(
        vmachine_id=vmachine_id,
        ip=ip,
        port=port,
        protocol=protocol
    )
