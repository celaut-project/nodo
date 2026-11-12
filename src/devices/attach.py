"""Attach devices to a guest and give them back (#536, phase P4).

A launch (and a wake, and a revive: the same ``execute``) of a service with devices:

1. :func:`acquire` picks free devices for the asked ones (``spec.assign``: distinct
   physical devices, one kernel) and reserves them in the assignment table, all or
   nothing. The pick is new at every start, so a woken instance may get another
   device of the same architecture. None free: the start is refused with the reason
   (a wake leaves the instance hibernated).
2. The guest boots the kernel of the picked entries (the exact hash of the tag),
   not the one in ``bundle.json``, and cloud-hypervisor gets one
   ``--device path=/sys/bus/pci/devices/<address>/`` value per device.
3. When the hypervisor process is gone (stop, kill, hibernate, a failed start),
   :func:`release` resets each device (FLR, through sysfs), checks that it still
   answers and is still on vfio-pci, quarantines it if not, and frees it.
"""
import os
from typing import List, Optional, Sequence

from protos import celaut_pb2
from src.devices import admission, assignments, host, inventory, spec
from src.utils import logger as log

# Reserve, read the free devices again and retry when another start took one first.
_RESERVE_ATTEMPTS = 3


def acquire(vmachine_id: str, service: Optional[celaut_pb2.Service]) -> List[spec.NodeDevice]:
    """Reserve devices for ``service`` as ``vmachine_id``; [] when it asks for none.

    Raises :class:`admission.UnsupportedDevicesException` with the reason when the
    node cannot give them now.
    """
    if not admission.has_devices(service):
        return []
    reason = spec.service_reason(service)
    if reason:
        raise admission.UnsupportedDevicesException(reason)
    if not inventory.enabled():
        raise admission.UnsupportedDevicesException(admission.NOT_ATTACHING_REASON)
    asked = list(service.container.devices)
    # A revive of an instance whose host process died without a release.
    assignments.release(vmachine_id)
    for _ in range(_RESERVE_ATTEMPTS):
        found, reason = spec.assign(asked, inventory.free_devices())
        if found is None:
            raise admission.UnsupportedDevicesException(reason)
        from src.devices import pricing

        if assignments.reserve(vmachine_id, found, [pricing.entry_rate(e) for e in found]):
            log.LOGGER(
                f"[devices][{vmachine_id}] reserved "
                + ", ".join(f"{e.address} ({e.chip})" for e in found)
                + f" with kernel {found[0].kernel_hash}"
            )
            return found
    raise admission.UnsupportedDevicesException(
        "Another instance took the free devices of these architectures; try again."
    )


def kernel_path(entries: Sequence[spec.NodeDevice]) -> Optional[str]:
    """The kernel that a guest with these devices boots (they share one)."""
    if not entries:
        return None
    from src.virtualizers.microvm import kernel_store

    return str(kernel_store.resolve(entries[0].kernel_hash))


def ch_args(entries: Sequence[spec.NodeDevice]) -> List[str]:
    """cloud-hypervisor ``--device``: one option, one value per device (like --disk)."""
    if not entries:
        return []
    return ["--device", *[f"path={host.device_dir(e.address)}/" for e in entries]]


def _reset(address: str) -> Optional[str]:
    """Reset a device through sysfs. None when it went well, else why it did not."""
    path = os.path.join(host.device_dir(address), "reset")
    if not os.path.exists(path):
        return None  # No reset method: doctor reports it; vfio-pci reset what it could.
    try:
        with open(path, "w", encoding="ascii") as f:
            f.write("1")
    except PermissionError:
        # --service-user mode: the file is root's. vfio-pci resets the device when
        # the hypervisor closes it, and the checks below still run.
        return None
    except OSError as e:
        return f"reset failed: {e}"
    return None


def health(address: str) -> Optional[str]:
    """None when the device is fit to give to the next tenant, else why not."""
    dev = host.read_device(address)
    if not dev.exists:
        return "the device is gone from the PCI bus"
    if dev.driver != host.VFIO_DRIVER:
        return f"the device is on the driver '{dev.driver or 'none'}', not vfio-pci"
    if not host.responds(address):
        return "the device does not answer config reads after the reset"
    return None


def _wait_gone(pid: int, timeout_s: float = 10.0) -> bool:
    """Wait until ``pid`` has exited (gone, or a zombie: its files are closed)."""
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="ascii", errors="replace") as f:
                state = f.read().rsplit(")", 1)[-1].split()[0]
        except (OSError, IndexError):
            return True
        if state in ("Z", "X"):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def release(vmachine_id: str, pid: int = 0) -> List[str]:
    """Reset, check and free the devices of ``vmachine_id``. ``pid``: the hypervisor
    process, waited for first (a SIGKILL is not instant, and vfio-pci gives the device
    back when its last file closes). A device that fails is quarantined, never offered
    again until the operator clears it. Returns the addresses freed."""
    held = assignments.of_instance(vmachine_id)
    if held and pid and not _wait_gone(pid):
        # Still running: resetting a device under a live guest would break it, and
        # freeing it would give it to two guests. Keep it held; the reconcile at the
        # next start, or the next kill, frees it.
        log.LOGGER(f"[devices][{vmachine_id}] pid {pid} still runs; keeping its devices")
        return []
    for row in held:
        address = row["address"]
        problem = _reset(address) or health(address)
        if problem:
            inventory.quarantine(address, f"{problem} (after instance {vmachine_id})")
            log.LOGGER(f"[devices][{vmachine_id}] quarantined {address}: {problem}")
    freed = assignments.release(vmachine_id)
    if freed:
        log.LOGGER(f"[devices][{vmachine_id}] released {', '.join(freed)}")
    return freed


def reconcile() -> List[str]:
    """At the node start: free the devices of instances with no hypervisor process.

    A node that stopped hard (power, OOM) never released them. Returns the instances.
    """
    from src.virtualizers.microvm.runtime_state import load_runtime_state

    stale = []
    for instance_id in sorted(set(assignments.assigned().values())):
        state = load_runtime_state(instance_id)
        pid = int((state or {}).get("pid") or 0)
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except PermissionError:
                alive = True  # Another user's process: it exists.
            except OSError:
                alive = False
        if not alive:
            release(instance_id)
            stale.append(instance_id)
    return stale
