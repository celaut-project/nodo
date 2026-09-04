"""The backends this node has, and how the node reaches them.

The neutral layer's whole knowledge of a backend: a name, the family it belongs
to, and the five lifecycle calls. Nothing about how it gets its work done -- no
base class to inherit, no shape to impersonate. A backend that boots a local
microVM and one that hands the work to somebody else's API sit in this table as
equals, because the table says nothing that only one of them can honour.

Two levels, and they answer different questions:

* **Backend** -- what runs *one* guest: ``execute``, ``kill``, ``maintain``,
  ``hotplug``. Routed to per instance, by the ``virtualizer`` column the launcher
  recorded before the guest existed.
* **Family** -- what a group of backends shares because they are the same kind of
  thing: the build cache they all boot out of, and the sweep that finds guests
  with no database row. Routed to per family, because the thing being swept is
  the family's store, not one backend's.

Splitting it there is what stopped ``kill``, ``maintain`` and the janitor from
existing once per backend, and what keeps the microVM machinery from becoming
"what every backend has". See ``docs/BACKENDS.md``.

Every entry is a module path and an attribute name, resolved on the call. That is
not laziness for its own sake: importing a launcher costs its whole dependency
tree (grpc, bee_rpc, the gateway), and this table is imported by everything that
needs to route anything -- the firewall frontend, ``nodo instances``, the
maintenance tick. It also means a backend may import the neutral layer freely,
which is what the old direct-import dispatch could not allow: ``ch.maintain``
importing ``qemu.process`` while ``qemu.execute`` imported ``ch.execute`` was a
cycle that had to be broken with function-local imports (#295).
"""
import importlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

CH = "ch"
QEMU = "qemu"

MICROVM = "microvm"


def _call(module: str, attribute: str, *, member: Optional[str] = None) -> Callable[..., Any]:
    """A callable that resolves ``module.attribute`` when it is first invoked.

    ``member`` names a microVM family member whose descriptor is passed ahead of
    the caller's arguments -- how one shared implementation is pointed at the
    hypervisor that asked for it, without the neutral layer knowing what a
    ``Hypervisor`` is or importing the table that lists them.
    """

    def invoke(*args: Any, **kwargs: Any) -> Any:
        target = getattr(importlib.import_module(module), attribute)
        if member is None:
            return target(*args, **kwargs)
        members = importlib.import_module("src.virtualizers.microvm.members")
        return target(getattr(members, member), *args, **kwargs)

    invoke.__name__ = f"{module.rsplit('.', 1)[-1]}.{attribute}"
    return invoke


@dataclass(frozen=True)
class Backend:
    """One backend: what it is called, whose family it is in, and its lifecycle."""

    name: str
    family: str
    execute: Callable[..., Any]
    kill: Callable[..., Any]
    maintain: Callable[..., Any]
    hotplug: Callable[..., Any]


@dataclass(frozen=True)
class Family:
    """What a group of backends shares because they are the same kind of thing.

    ``build``/``is_built``/``remove_built``/``built_rootfs_size_bytes`` are here
    rather than on ``Backend`` because there is one build cache for the family:
    QEMU boots the bundles the microVM builder wrote, so there is nothing to route
    between. A family whose backends do not build anything locally -- a remote one
    -- would answer these by asking the service that does.

    ``sweep_orphans`` is here for the same reason: what it sweeps is the family's
    store. A backend cannot sweep alone without either reading its siblings'
    entries or missing the ones it did not write.
    """

    name: str
    build: Callable[..., Any]
    is_built: Callable[..., Any]
    remove_built: Callable[..., Any]
    built_rootfs_size_bytes: Callable[..., Any]
    billable_resources: Callable[..., Any]
    sweep_orphans: Callable[..., Any]


MICROVM_FAMILY = Family(
    name=MICROVM,
    build=_call("src.virtualizers.microvm.build", "build"),
    is_built=_call("src.virtualizers.microvm.build", "is_service_built"),
    remove_built=_call("src.virtualizers.microvm.build", "remove_built_service"),
    built_rootfs_size_bytes=_call("src.virtualizers.microvm.build", "built_rootfs_size_bytes"),
    billable_resources=_call("src.virtualizers.microvm.limits", "billable_resources"),
    sweep_orphans=_call("src.virtualizers.microvm.maintain", "sweep_orphans"),
)

FAMILIES: Dict[str, Family] = {MICROVM_FAMILY.name: MICROVM_FAMILY}

BACKENDS: Dict[str, Backend] = {
    CH: Backend(
        name=CH,
        family=MICROVM,
        execute=_call("src.virtualizers.ch.execute", "execute"),
        kill=_call("src.virtualizers.microvm.kill", "kill", member="CH"),
        maintain=_call("src.virtualizers.microvm.maintain", "maintain", member="CH"),
        hotplug=_call("src.virtualizers.ch.hotplug", "hotplug"),
    ),
    QEMU: Backend(
        name=QEMU,
        family=MICROVM,
        execute=_call("src.virtualizers.qemu.execute", "execute"),
        kill=_call("src.virtualizers.microvm.kill", "kill", member="QEMU"),
        maintain=_call("src.virtualizers.microvm.maintain", "maintain", member="QEMU"),
        hotplug=_call("src.virtualizers.qemu.hotplug", "hotplug"),
    ),
}

DEFAULT_BACKEND = CH


def normalize(name: Optional[str]) -> Optional[str]:
    """A configured or recorded backend name, or ``None`` if it names none.

    Accepts the spellings that reach this node from a config file or an old
    database row. Returns ``None`` rather than defaulting, so a caller has to say
    what it wants done with a name nobody claims -- guessing CH for an
    unrecognized one is what reaped a healthy QEMU guest (#295).
    """
    if not isinstance(name, str):
        return None
    value = name.strip().lower()
    if value in {"ch", "cloud_hypervisor", "cloud-hypervisor"}:
        return CH
    if value == QEMU:
        return QEMU
    return None


def backend(name: Optional[str]) -> Backend:
    """The backend ``name`` refers to. Raises for a name this node does not have."""
    resolved = normalize(name)
    if resolved is None:
        raise ValueError(f"Unknown virtualizer {name!r}. Supported: {', '.join(BACKENDS)}.")
    return BACKENDS[resolved]


def family_of(name: Optional[str]) -> Family:
    """The family of the backend ``name`` refers to."""
    return FAMILIES[backend(name).family]
