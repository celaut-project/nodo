"""The node's only door to a virtualizer.

Everything outside this package asks here, and this module asks
``src.virtualizers.registry``. Nothing else in the node imports a backend or a
backend family directly -- not the maintenance tick, not ``nodo prune``, not the
firewall frontend -- because every place that did became a place where one
backend's implementation was quietly treated as the node's behaviour: the janitor
was reached at ``ch.maintain.janitor_cleanup_orphans``, so QEMU guests were swept
by CH's teardown and logged as CH's (#295, #299).

Routing, and what each call routes *by*:

* ``execute`` -- by the service, through :func:`select_virtualizer`, which is also
  what the launcher used to fill the ``virtualizer`` column before the guest
  existed, so the row and the running guest never disagree.
* ``kill`` / ``maintain`` / ``hotplug`` -- by that recorded column, so a guest is
  always torn down and health-checked by the backend that booted it.
* ``build`` / ``is_built`` / ``remove_built_service`` /
  ``resolve_billable_resources`` -- by *family*, not by backend: there is one
  build cache per family and both microVM backends boot out of it.
* ``janitor_cleanup_orphans`` -- by family too, and per family rather than per
  backend, because what is being swept is the family's store.

Two backends are supported today, chosen per service by architecture:

* **Cloud Hypervisor (CH)** -- the default. Boots a service as a microVM under
  KVM, which only runs a guest of the host's own architecture. Native services
  always take this path, so native performance never regresses.
* **QEMU** -- the opt-in cross-arch path (``virtualizers.qemu.ENABLE``). Boots a
  foreign-arch service under TCG software emulation (e.g. an arm64 service on an
  x86_64 node). Slow, so it is only chosen when the service arch differs from the
  host and emulation is enabled and available.

Both are members of the ``microvm`` family (``src/virtualizers/microvm/``), which
holds what they share because they are the same kind of thing. The Docker
virtualizer was removed, so the node no longer depends on a local Docker install;
service packing is delegated to the external packer-service.
"""
from typing import Callable, Dict, Optional, Tuple

from protos import celaut_pb2
from src.database.sql_connection import SQLConnection
from src.utils.logger import LOGGER as log
from src.virtualizers.architecture import check_supported_architecture, UnsupportedArchitectureException
from src.virtualizers.firewall import TransportProtocol, remove_rule as vm_remove_rule
from src.virtualizers.registry import BACKENDS, DEFAULT_BACKEND, FAMILIES, backend, normalize
from src.virtualizers.selection import select_virtualizer

sc = SQLConnection()


def get_configured_virtualizer() -> str:
    """Default backend name (the native one).

    The *actual* backend for a launch is chosen per service by
    :func:`src.virtualizers.selection.select_virtualizer`; this remains the
    fallback used where no service is in hand.
    """
    return DEFAULT_BACKEND


def _backend_of(vmachine_id: str):
    """The backend that launched ``vmachine_id``, from the database.

    Defaults to the native one for a row that records nothing recognizable, which
    is the only place in the node where defaulting is right: this answers "who
    should act on this instance", and the alternative -- refusing to act -- leaves
    a running guest nobody will ever kill. Every reader that judges a *guest* (the
    janitor, the health check) reads the recorded facts instead and refuses to
    guess; see ``microvm.members.member``.
    """
    try:
        recorded = normalize(sc.get_internal_virtualizer(id=vmachine_id))
    except Exception:
        recorded = None
    return BACKENDS[recorded or DEFAULT_BACKEND]


def _default_family():
    """The family of the native backend.

    Which is all the build-cache calls can route by today, and the reason is not
    only that there is one family. Three of them hold a service *hash* and
    nothing else -- the hash says nothing about which backend would run it -- and
    their answers are not the kind that merge across families: a price is one
    number, not a set. A second family would have to settle that, and settle what
    ``build`` means when the node cannot run what it is building. See
    ``docs/BACKENDS.md``.
    """
    return FAMILIES[BACKENDS[DEFAULT_BACKEND].family]


def is_built(service_hash: str) -> bool:
    """Check if a service with the given hash is already built."""
    return _default_family().is_built(service_hash)


def remove_built_service(service_hash: str) -> int:
    """Delete what building this service left on disk; return the bytes freed."""
    return _default_family().remove_built(service_id=service_hash)


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
    family = _default_family()
    return family.billable_resources(
        resources,
        built_rootfs_size_bytes=family.built_rootfs_size_bytes(service_hash) if service_hash else None,
    )


def build(
        service: celaut_pb2.Service,
        metadata: celaut_pb2.Metadata,
        service_id: Optional[str] = None,
) -> str:
    """Build a service and return its identifier.

    Deliberately *not* routed through :func:`select_virtualizer`, tempting as it
    is: that raises when a service's architecture is one this node cannot run,
    and building is not running. A node may hold and serve a foreign-arch build
    with emulation switched off. What decides the format here is the family, and
    there is one.
    """
    if not check_supported_architecture(service=service, metadata=metadata):
        log(f'Build process of {service_id}: unsupported architecture.')
        raise UnsupportedArchitectureException(arch=str(metadata))

    return _default_family().build(
        service=service,
        metadata=metadata,
        service_id=service_id,
    )


def hotplug(
        vmachine_id: str,
        system_requeriments_range: celaut_pb2.ModifyServiceSystemResourcesInput
) -> bool:
    """Modify the system requirements of a running service.

    Routed per instance rather than shared, because the resize knob is a property
    of the hypervisor: CPU enforcement is cgroup-based for both, but MEMORY is
    not. A QEMU guest boots with a fixed ``-m`` and only a ``virtio-balloon``
    resize (over QMP) actually returns guest RAM, whereas tightening the process
    cgroup alone swaps or OOM-kills qemu without resizing the guest.
    """
    return _backend_of(vmachine_id).hotplug(
        vmachine_id=vmachine_id,
        system_requeriments_range=system_requeriments_range,
    )


def kill(vmachine_id: str) -> bool:
    """Kill a running service, routed to the backend that launched it."""
    return _backend_of(vmachine_id).kill(vmachine_id=vmachine_id)


def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    """Check the status of a running service and remove it if it has exited."""
    _backend_of(vmachine_id).maintain(
        vmachine_id=vmachine_id,
        debug_mode=debug_mode,
        remove_and_penalize=remove_and_penalize,
    )


def janitor_cleanup_orphans(debug_mode: bool = False) -> None:
    """Reclaim what is running or on disk with no database row behind it.

    The counterpart to :func:`maintain`, and it has the opposite problem: maintain
    is asked about instances the database knows about, while this looks for the
    ones it does not, so it cannot ask the database who owns what.

    Asked of each *family* rather than each backend, and each family answers in
    whatever way suits what it actually has. The microVM family enumerates the one
    runtime-state store its members share and dispatches each entry to the
    hypervisor that wrote it. A family with no local store -- a remote backend
    whose runtime state is a handle to someone else's API -- would answer the same
    question by querying that API, and would never read a directory.

    Never propagates a family's failure: one family's sweep failing must not stop
    the others, and the maintenance tick that calls this has instances to charge
    afterwards.
    """
    for family in FAMILIES.values():
        try:
            family.sweep_orphans(debug_mode=debug_mode)
        except Exception as e:
            log(f"[{family.name}][janitor] failed: {e}")


def execute(
        assigment_ports: Optional[Dict[int, int]],
        by_local: bool,
        service_id: str,
        service: celaut_pb2.Service,
        config: Optional[celaut_pb2.Configuration],
        system_resources: celaut_pb2.Service.Container.Resources,
        father_id: str,
        register_instance: Optional[Callable[[str, str, celaut_pb2.Sysresources], None]] = None,
) -> Tuple[str, str, celaut_pb2.Sysresources]:
    """
    Execute a built service and return (vmachine_id, vmachine_ip, resolved_resources).

    ``system_resources`` is the whole declared range, ``at_init`` *and* ``at_most``,
    because which end a backend has to act on at launch is a property of the
    backend, not of the manifest: CH resizes memory by moving the cgroup, so it
    starts a guest at ``at_init`` and raises the ceiling whenever asked, while
    QEMU's ``-m`` is fixed for the life of the process -- a QEMU guest that was not
    *booted* with room for ``at_most`` can never be grown into it, so it reserves
    the ceiling up front and has its balloon hold the difference.

    ``resolved_resources`` is what the virtualizer actually reserved for the guest --
    defaults and floors already applied -- so the launcher persists what the instance
    holds rather than what its manifest requested (#249). A field left at 0 means the
    virtualizer does not resolve it, and the launcher falls back to the manifest. Note
    "holds", not "was booted with": a QEMU guest whose balloon has already taken the
    headroom back resolves to ``at_init``, and only a guest that kept it resolves to
    the ceiling it was booted with.

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
    return backend(select_virtualizer(service=service)).execute(
        assigment_ports=assigment_ports,
        by_local=by_local,
        service_id=service_id,
        service=service,
        config=config,
        system_resources=system_resources,
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
