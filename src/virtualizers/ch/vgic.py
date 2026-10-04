"""Whether this host's KVM can give a Cloud Hypervisor guest its interrupt controller.

On aarch64, Cloud Hypervisor's KVM backend knows exactly one interrupt controller:
an in-kernel GICv3 with an ITS (``KvmGicV3Its``: ``KVM_DEV_TYPE_ARM_VGIC_V3``, then
``KVM_DEV_TYPE_ARM_VGIC_ITS``). There is no GICv2 path and no flag to ask for one.
KVM registers the vGIC device types it can emulate from the host's own GIC, so on a
host whose GIC is a GICv2 -- the GIC-400 of a Raspberry Pi 4 or 5, and many other
SoCs -- or a nested guest whose hypervisor only exposes one, ``KVM_CREATE_DEVICE``
answers ENODEV and CH dies before the guest kernel runs a single instruction::

    Fatal error: VmBoot(VmBoot(DeviceManager(CreateInterruptController(CreateGic(
    CreateVgic(Vgic error CreateGic(CreateDevice(No such device (os error 19)))))))))

That is a property of the host, not of the service, so every native launch on it
fails the same way (issue #440). This module asks KVM the same question CH does,
with ``KVM_CREATE_DEVICE_TEST`` so nothing is created, and gives the launcher, the
architecture table and ``nodo doctor`` one answer and one explanation.

Deliberately stdlib-only, like ``microvm.guest``: doctor imports it.
"""
import errno
import fcntl
import os
import platform
import struct
from dataclasses import dataclass

KVM_PATH = "/dev/kvm"

# <linux/kvm.h>. arm64 uses the asm-generic ioctl encoding.
KVM_CREATE_VM = 0xAE01  # _IO(KVMIO, 0x01)
KVM_CREATE_DEVICE = 0xC00CAEE0  # _IOWR(KVMIO, 0xe0, struct kvm_create_device)
KVM_CREATE_DEVICE_TEST = 1
KVM_DEV_TYPE_ARM_VGIC_V2 = 5
KVM_DEV_TYPE_ARM_VGIC_V3 = 7
KVM_DEV_TYPE_ARM_VGIC_ITS = 8

OK = "ok"
MISSING = "missing"
NOT_APPLICABLE = "not_applicable"
UNKNOWN = "unknown"

GUIDANCE = (
    "This host's KVM cannot create the GICv3/ITS interrupt controller Cloud "
    "Hypervisor requires on arm64 (its GIC is a GICv2, e.g. a Raspberry Pi's "
    "GIC-400, or it is a nested/cloud VM that does not expose a vGICv3). No "
    "native arm64 microVM can boot here; this is a host limitation, not a service "
    "or nodo configuration problem. Run nodo on an arm64 host with a GICv3 "
    "(Ampere, Graviton bare-metal, Apple Silicon under Asahi, ...); "
    "`dmesg | grep -i gic` shows which GIC this host has."
)


@dataclass(frozen=True)
class VgicProbe:
    status: str
    detail: str

    @property
    def missing(self) -> bool:
        return self.status == MISSING


def _device_supported(vm_fd: int, device_type: int) -> bool:
    """Whether KVM has ops for ``device_type``; raises OSError on anything but ENODEV."""
    request = bytearray(struct.pack("III", device_type, 0, KVM_CREATE_DEVICE_TEST))
    try:
        fcntl.ioctl(vm_fd, KVM_CREATE_DEVICE, request, True)
    except OSError as e:
        if e.errno == errno.ENODEV:
            return False
        raise
    return True


def probe(machine: str = "", kvm_path: str = KVM_PATH) -> VgicProbe:
    """Ask KVM whether it can create the vGIC Cloud Hypervisor will ask for.

    ``MISSING`` only when KVM positively answers ENODEV, which is the exact failure
    CH hits. Anything that stops the question being asked -- no /dev/kvm, no
    permission, a VM that cannot be created -- is ``UNKNOWN``: other checks own
    those, and a node must not stop advertising its own architecture on a guess.
    """
    machine = (machine or platform.machine()).lower()
    if machine not in ("aarch64", "arm64"):
        return VgicProbe(NOT_APPLICABLE, f"no vGIC is needed on {machine}")

    try:
        kvm_fd = os.open(kvm_path, os.O_RDWR | os.O_CLOEXEC)
    except OSError as e:
        return VgicProbe(UNKNOWN, f"cannot open {kvm_path}: {e}")
    vm_fd = -1
    try:
        vm_fd = fcntl.ioctl(kvm_fd, KVM_CREATE_VM, 0)
        if not _device_supported(vm_fd, KVM_DEV_TYPE_ARM_VGIC_V3):
            has_v2 = _device_supported(vm_fd, KVM_DEV_TYPE_ARM_VGIC_V2)
            return VgicProbe(
                MISSING,
                "KVM cannot create a GICv3 (KVM_DEV_TYPE_ARM_VGIC_V3: ENODEV)"
                + ("; it only offers a GICv2" if has_v2 else ""),
            )
        if not _device_supported(vm_fd, KVM_DEV_TYPE_ARM_VGIC_ITS):
            return VgicProbe(MISSING, "KVM cannot create a GICv3 ITS (KVM_DEV_TYPE_ARM_VGIC_ITS: ENODEV)")
        return VgicProbe(OK, "KVM can create a GICv3 with ITS")
    except OSError as e:
        return VgicProbe(UNKNOWN, f"KVM vGIC probe failed: {e}")
    finally:
        if vm_fd >= 0:
            os.close(vm_fd)
        os.close(kvm_fd)


def is_vgic_failure(stderr: str) -> bool:
    """Whether CH's stderr is the interrupt-controller failure described above."""
    return "CreateVgic" in stderr or (
        "CreateInterruptController" in stderr and "os error 19" in stderr
    )
