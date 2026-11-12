"""A node that cannot attach the devices of a service refuses it, never ignores them.

The same rule as for an architecture and a notice (#645): every place that says
"this node does not run this service" already handles an
``UnsupportedArchitectureException``, so the refusal is one.
"""
from typing import Optional

from protos import celaut_pb2
from src.devices import spec
from src.virtualizers.architecture import UnsupportedArchitectureException

NATIVE_ONLY_REASON = (
    "A device is available only with the native architecture of the node: the QEMU "
    "emulation path does not attach devices."
)
NOT_ATTACHING_REASON = "This node does not attach devices."


class UnsupportedDevicesException(UnsupportedArchitectureException):
    """The service asks for devices that this node cannot give."""

    def __init__(self, reason: str):
        self.message = reason
        Exception.__init__(self, self.message)


def has_devices(service: Optional[celaut_pb2.Service]) -> bool:
    # Anything that is not a Service message (a placeholder in a caller or a test)
    # asks for no device.
    if not isinstance(service, celaut_pb2.Service):
        return False
    return bool(service.HasField("container") and len(service.container.devices))


def refusal(service: Optional[celaut_pb2.Service], virtualizer: Optional[str] = None) -> Optional[str]:
    """Why this node refuses the devices of ``service``, or None (also with no devices)."""
    if not has_devices(service):
        return None
    reason = spec.service_reason(service)
    if reason:
        return reason
    if virtualizer is not None and virtualizer != "ch":
        return NATIVE_ONLY_REASON
    return NOT_ATTACHING_REASON


def enforce_supported(service: Optional[celaut_pb2.Service], virtualizer: Optional[str] = None) -> None:
    reason = refusal(service, virtualizer)
    if reason:
        raise UnsupportedDevicesException(reason)


def request_availability(main: celaut_pb2.ArchitectureResources, devices) -> dict:
    """The GetResourceAvailability answer for a request that names devices.

    The main resources and the devices are answered together: a "yes" means that one
    instance with both fits now. Devices go only with the native architecture, and
    each asked device needs a free physical device of its own, all with one kernel.
    """
    reason = spec.devices_reason(devices)
    if reason:
        return {"can_execute": False, "reason": reason}
    from src.devices import inventory

    if not inventory.enabled():
        return {"can_execute": False, "reason": NOT_ATTACHING_REASON}
    tags = list(main.architecture.tags)
    if tags:
        from src.utils.arch_guard import arch_from_tags, host_arch_tag

        if arch_from_tags(tags) != host_arch_tag():
            return {"can_execute": False, "reason": NATIVE_ONLY_REASON}
    found, reason = spec.assign(devices, inventory.free_devices())
    if found is None:
        return {"can_execute": False, "reason": reason}
    from src.utils.cost_functions.resource_availability import get_architecture_availability

    return get_architecture_availability(main)
