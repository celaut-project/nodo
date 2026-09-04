"""Where the microVM family keeps things under ``CACHE``.

One module so the layout is stated once. It used to be fifteen copies of
``Path(CACHE) / "cloud_hypervisor" / ...`` spread across both backends' execute,
kill, build and state modules -- one backend's name over two backends' data, and
no single place to read the layout off.

::

    CACHE/microvm/
      <service_id>/<arch>/     built bundle: rootfs.ext4, kernel, initramfs, bundle.json
      runtime/                 one <vmachine_id>.json per VM, plus <vmachine_id>/ per VM
      failures/                runtime dirs kept for debugging on a failed launch
      shared_fs/               virtiofs share trees
"""
from pathlib import Path
from typing import Optional

from src.utils.config import ConfigManager
from src.virtualizers.microvm.errors import MicroVMError

env_manager = ConfigManager()

FAMILY_DIR_NAME = "microvm"

# Names directly under the family root that are not service bundles.
NON_BUNDLE_ENTRIES = frozenset({"runtime", "failures", "shared_fs"})


def cache_root() -> str:
    cache = env_manager.get("CACHE")
    if not cache:
        raise MicroVMError("CACHE path is not configured.")
    return str(cache)


def family_root() -> Path:
    """``CACHE/microvm`` -- everything below belongs to this family, not to CH."""
    return Path(cache_root()) / FAMILY_DIR_NAME


def optional_family_root() -> Optional[Path]:
    """:func:`family_root`, or ``None`` when ``CACHE`` is unset.

    For readers that must degrade rather than raise: a disk report on a node with
    no cache configured has nothing to report, not an error to raise.
    """
    try:
        return family_root()
    except MicroVMError:
        return None


def service_root(service_id: str) -> Path:
    return family_root() / service_id


def bundle_dir(service_id: str, arch: str) -> Path:
    return service_root(service_id) / arch


def runtime_root() -> Path:
    """Where every VM's state file and runtime directory live.

    Shared by the whole family on purpose: one bridge, one subnet, one IP/MAC
    allocator that reads it, one janitor that sweeps it. Two backends handing out
    addresses from stores they cannot see each other's entries in would collide.
    """
    return family_root() / "runtime"


def runtime_vm_dir(vmachine_id: str) -> Path:
    return runtime_root() / vmachine_id


def runtime_state_file(vmachine_id: str) -> Path:
    return runtime_root() / f"{vmachine_id}.json"


def failures_root() -> Path:
    return family_root() / "failures"


def shared_fs_root() -> Path:
    return family_root() / "shared_fs"


def control_socket_dir() -> Path:
    """Where the hypervisors' control sockets go.

    Deliberately not under ``runtime/``: an ``AF_UNIX`` path is capped at 108
    bytes, and a runtime directory is nested under ``CACHE`` and keyed by the
    full 64-hex ``vmachine_id``, which on its own can exceed that. So the sockets
    live in a short, flat directory and carry a truncated id.
    """
    return Path(env_manager.get("virtualizers.ch.API_SOCKET_DIR", "/tmp/nodo-ch"))


def control_socket_path(socket_prefix: str, vmachine_id: str) -> Path:
    return control_socket_dir() / f"{socket_prefix}{vmachine_id[:16]}.sock"
