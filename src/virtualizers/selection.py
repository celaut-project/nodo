"""Choose which virtualizer backend runs a service: native CH or emulated QEMU.

One decision, made in one place, used by two callers that must agree:

* :func:`src.gateway.launcher.local_execution` records the chosen backend in the
  ``local_instances.virtualizer`` column *before* the guest exists, so later
  ``kill``/``maintain``/firewall dispatch can route by it.
* :func:`src.virtualizers.interface.execute` dispatches the actual launch.

Both call :func:`select_virtualizer` on the same ``service``, so the column and
the launch never disagree.

Rule:

* service arch == host arch  -> ``"ch"``  (Cloud Hypervisor under KVM; unchanged,
  and still the default so native performance never regresses).
* service arch != host arch  -> ``"qemu"`` when emulation is enabled and the
  emulator + guest kernel/initramfs for that arch are present; otherwise the
  historical behaviour: :class:`UnsupportedArchitectureException`.
"""
from typing import Optional

from protos import celaut_pb2
from src.utils.arch_guard import host_arch_tag
from src.virtualizers.architecture import (
    UnsupportedArchitectureException,
    get_arch_tag,
)
from src.virtualizers.qemu.config import emulation_ready
from src.virtualizers.registry import CH, QEMU


def select_virtualizer(
    service: celaut_pb2.Service,
    metadata: Optional[celaut_pb2.Metadata] = None,
) -> str:
    """Return ``"ch"`` or ``"qemu"`` for ``service``.

    Raises :class:`UnsupportedArchitectureException` when the service arch differs
    from the host and emulation is not available -- exactly what a cross-arch
    request did before this backend existed.
    """
    service_arch = get_arch_tag(service=service, metadata=metadata)
    host_arch = host_arch_tag()

    # Unknown service arch or undetectable host arch: fall back to CH, whose own
    # arch resolution then raises the canonical UnsupportedArchitectureException.
    if not service_arch or not host_arch:
        return CH

    if service_arch == host_arch:
        return CH

    if emulation_ready(service_arch):
        return QEMU

    raise UnsupportedArchitectureException(arch=service_arch)
