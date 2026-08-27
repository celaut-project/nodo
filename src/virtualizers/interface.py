from src.virtualizers.ch.build import is_service_built as ch_is_service_built
from src.virtualizers.ch.build import build as ch_build
from src.virtualizers.ch.build import built_rootfs_size_bytes as ch_built_rootfs_size_bytes
from src.virtualizers.ch.build import remove_built_service as ch_remove_built_service
from src.virtualizers.ch.limits import billable_resources as ch_billable_resources
from src.virtualizers.ch.execute import execute as ch_execute
from src.virtualizers.ch.hotplug import hotplug as ch_hotplug
from src.virtualizers.ch.kill import kill as ch_kill
from src.virtualizers.ch.maintain import maintain as ch_maintain
from src.virtualizers.qemu.execute import execute as qemu_execute
from src.virtualizers.qemu.kill import kill as qemu_kill
from src.virtualizers.qemu.maintain import maintain as qemu_maintain
from src.virtualizers.qemu.hotplug import hotplug as qemu_hotplug
from src.virtualizers.selection import CH, QEMU, select_virtualizer
from src.virtualizers.firewall import TransportProtocol, remove_rule as vm_remove_rule
from typing import Optional, Callable, Dict, Tuple
from src.virtualizers.architecture import check_supported_architecture, UnsupportedArchitectureException
from protos import celaut_pb2
from src.utils.logger import LOGGER as log
from src.utils.config import ConfigManager
from src.database.sql_connection import SQLConnection


"""
This interface defines the functions that any virtualizer implementation must provide.

Two virtualizers are supported, chosen per service by architecture:

* **Cloud Hypervisor (CH)** — the default. Boots a service as a microVM under KVM,
  which only runs a guest of the host's own architecture. Native services always
  take this path, so native performance never regresses.
* **QEMU** — the opt-in cross-arch path (``virtualizers.qemu.ENABLE``). Boots a
  foreign-arch service under TCG software emulation (e.g. an arm64 service on an
  x86_64 node). Slow, so it is only chosen when the service arch differs from the
  host and emulation is enabled and available.

The Docker virtualizer was removed so the node no longer depends on a local
Docker install; service packing is delegated to the external packer-service.
"""

env_manager = ConfigManager()
sc = SQLConnection()

def get_configured_virtualizer() -> str:
    """Default virtualizer name (the native backend).

    The *actual* backend for a launch is chosen per service by
    :func:`src.virtualizers.selection.select_virtualizer`; this remains the
    fallback used where no service is in hand.
    """
    return CH


def _resolve_instance_virtualizer(vmachine_id: str) -> str:
    """Which backend launched ``vmachine_id`` (from the DB), defaulting to CH.

    Lifecycle calls (kill/maintain/hotplug) route by this so a QEMU guest is torn
    down and health-checked by the QEMU backend, never CH's.
    """
    try:
        recorded = sc.get_internal_virtualizer(id=vmachine_id)
        if isinstance(recorded, str) and recorded.strip().lower() == QEMU:
            return QEMU
    except Exception:
        pass
    return CH

def is_built(service_hash: str) -> bool:
    """Check if a service with the given hash is already built."""
    return ch_is_service_built(service_hash)

def remove_built_service(service_hash: str) -> int:
    """Delete what building this service left on disk; return the bytes freed.

    One implementation for both backends, because there is only one build cache:
    QEMU boots the bundles CH builds (``qemu/execute.py`` loads them through
    ``ch_exec._load_bundle``), so there is nothing to route between.
    """
    return ch_remove_built_service(service_id=service_hash)

def resolve_billable_resources(
        resources: celaut_pb2.Sysresources,
        service_hash: Optional[str] = None,
) -> celaut_pb2.Sysresources:
    """What an instance requesting `resources` will actually be billed for.

    The counterpart to ``execute``'s ``resolved_resources``, for callers that have to
    put a price on a manifest before any instance exists: quotes
    (``GetServiceEstimatedCost``) and the balance a new instance is funded with
    (``manager.default_initial_balance``). Both resolve the manifest the way
    ``execute`` resolves a guest, so what is quoted is what the maintenance tick then
    charges the row -- including for a service that asks for less than a floor: no CPU
    at all, sub-MIN_MEM_MIB RAM, a rootfs under MIN_ROOTFS_BYTES.

    Pass ``service_hash`` when it is known: a service already built here reports the
    exact image its instances receive, so the quote is that figure rather than the
    floor.
    """
    return ch_billable_resources(
        resources,
        built_rootfs_size_bytes=ch_built_rootfs_size_bytes(service_hash) if service_hash else None,
    )

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
    """Modify the system requirements of a running service.

    CPU enforcement is cgroup-based and shared, but MEMORY is not: a QEMU guest
    boots with a fixed ``-m`` and only a ``virtio-balloon`` resize (over QMP)
    actually returns guest RAM, whereas tightening the process cgroup alone
    swaps or OOM-kills qemu without resizing the guest. So QEMU instances route
    to their own hotplug (balloon for memory, cgroup for CPU); CH is unchanged.
    """
    if _resolve_instance_virtualizer(vmachine_id) == QEMU:
        return qemu_hotplug(
            vmachine_id=vmachine_id,
            system_requeriments_range=system_requeriments_range,
        )
    return ch_hotplug(
        vmachine_id=vmachine_id,
        system_requeriments_range=system_requeriments_range,
    )

def kill(vmachine_id: str) -> bool:
    """Kill a running service, routed to the backend that launched it."""
    if _resolve_instance_virtualizer(vmachine_id) == QEMU:
        return qemu_kill(vmachine_id=vmachine_id)
    return ch_kill(vmachine_id=vmachine_id)

def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    """Check the status of a running service and remove it if it has exited."""
    if _resolve_instance_virtualizer(vmachine_id) == QEMU:
        qemu_maintain(
            vmachine_id=vmachine_id,
            debug_mode=debug_mode,
            remove_and_penalize=remove_and_penalize,
        )
        return
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
        register_instance: Optional[Callable[[str, str, celaut_pb2.Sysresources], None]] = None,
) -> Tuple[str, str, celaut_pb2.Sysresources]:
    """
    Execute a built service and return (vmachine_id, vmachine_ip, resolved_resources).

    ``resolved_resources`` is what the virtualizer actually reserved for the guest --
    defaults and floors already applied -- so the launcher persists what the instance
    holds rather than what its manifest requested (#249). A field left at 0 means the
    virtualizer does not resolve it, and the launcher falls back to the manifest.

    ``register_instance`` is how the launcher gets those three values *before* this
    returns: every backend calls it the instant the guest starts running, which is
    also the instant the guest can call the node back. Waiting for the return value
    to record the instance left a window in which the node could not tell who was
    calling it (see the backends' own docstrings).

    The backend is chosen per service by :func:`select_virtualizer` on the same
    ``service`` the launcher used to record the ``virtualizer`` column, so the row
    and the running guest never disagree. A native-arch service takes the CH path
    unchanged; a foreign-arch service takes QEMU when emulation is enabled and
    available, else ``select_virtualizer`` raises ``UnsupportedArchitectureException``.
    """
    if select_virtualizer(service=service) == QEMU:
        return qemu_execute(
            assigment_ports=assigment_ports,
            by_local=by_local,
            service_id=service_id,
            service=service,
            config=config,
            initial_system_resources=initial_system_resources,
            father_id=father_id,
            register_instance=register_instance,
        )
    return ch_execute(
        assigment_ports=assigment_ports,
        by_local=by_local,
        service_id=service_id,
        service=service,
        config=config,
        initial_system_resources=initial_system_resources,
        father_id=father_id,
        register_instance=register_instance,
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
