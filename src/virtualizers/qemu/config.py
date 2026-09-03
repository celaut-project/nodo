"""QEMU emulation backend configuration and asset resolution.

The QEMU backend is the *cross-arch* launch path: it boots a service whose
architecture differs from the host under TCG (software emulation), where Cloud
Hypervisor cannot help because CH only runs a guest of the host architecture
under KVM. It is **opt-in** (``virtualizers.qemu.ENABLE``, default false) because
TCG is an order of magnitude slower than KVM -- native services must keep taking
the CH path.

The guest kernel/initramfs are the same per-arch assets CH uses
(``virtualizers.ch.KERNEL_PATHS`` / ``INITRAMFS_PATHS``, made cross-arch-available
by the prebuilt-guest-kernels work, celaut-project/nodo#271). A QEMU-specific
override (``virtualizers.qemu.KERNEL_PATHS`` / ``INITRAMFS_PATHS``) is honored when
present, otherwise the CH paths are reused unchanged.
"""
import os
import shutil
from typing import Dict, Optional

from src.utils.arch_guard import QEMU_SYSTEM_BINARIES as _QEMU_SYSTEM_BINARIES
from src.utils.config import ConfigManager

env_manager = ConfigManager()

# Canonical host/guest arch tag -> qemu-system emulator binary name. Defined in
# `src.utils.arch_guard`, which `commands.doctor` can import and this module cannot
# be imported by, and re-exported here under the name every call site already uses.
QEMU_SYSTEM_BINARIES: Dict[str, str] = _QEMU_SYSTEM_BINARIES

# qemu ``-machine`` type and default serial console device per guest arch. The
# console name has to match the kernel cmdline ``console=`` token or the guest
# never prints init output to the serial log we capture.
QEMU_MACHINE_BY_ARCH: Dict[str, str] = {
    "linux/amd64": "q35",
    "linux/arm64": "virt",
}
QEMU_CONSOLE_BY_ARCH: Dict[str, str] = {
    "linux/amd64": "ttyS0",
    "linux/arm64": "ttyAMA0",
}


def qemu_enabled() -> bool:
    """Whether the operator opted into emulated cross-arch execution."""
    return bool(env_manager.get("virtualizers.qemu.ENABLE", False))


def _binary_paths() -> Dict[str, str]:
    return env_manager.get("virtualizers.qemu.BINARY_PATHS", {}) or {}


def qemu_system_binary(arch: str) -> Optional[str]:
    """Resolve the ``qemu-system-<arch>`` binary for a guest ``arch``.

    An explicit ``virtualizers.qemu.BINARY_PATHS[arch]`` wins (and must be an
    executable file); otherwise the well-known emulator name is looked up on
    ``PATH``. Returns None when no usable emulator can be found.
    """
    configured = _binary_paths().get(arch)
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        return None

    name = QEMU_SYSTEM_BINARIES.get(arch)
    if not name:
        return None
    return shutil.which(name)


def qemu_kernel_path(arch: str) -> Optional[str]:
    """Guest kernel image for ``arch``: QEMU override first, else the CH asset."""
    override = (env_manager.get("virtualizers.qemu.KERNEL_PATHS", {}) or {}).get(arch)
    if override:
        return override
    return (env_manager.get("virtualizers.ch.KERNEL_PATHS", {}) or {}).get(arch)


def qemu_initramfs_path(arch: str) -> Optional[str]:
    """Guest initramfs for ``arch``: QEMU override first, else the CH asset."""
    override = (env_manager.get("virtualizers.qemu.INITRAMFS_PATHS", {}) or {}).get(arch)
    if override:
        return override
    return (env_manager.get("virtualizers.ch.INITRAMFS_PATHS", {}) or {}).get(arch)


def guest_assets_available(arch: str) -> bool:
    """True when both a guest kernel and initramfs exist on disk for ``arch``.

    The advertising side (which emulated arches to declare executable) and the
    selection side (native vs emulated) both gate on this so the node never
    claims or picks an emulated arch whose boot assets are missing.
    """
    kernel = qemu_kernel_path(arch)
    initramfs = qemu_initramfs_path(arch)
    return bool(
        kernel
        and initramfs
        and os.path.isfile(kernel)
        and os.path.isfile(initramfs)
    )


def emulation_ready(arch: str) -> bool:
    """Whether an emulated guest of ``arch`` can actually be launched here:
    emulation enabled, the emulator binary present, and boot assets on disk."""
    return (
        qemu_enabled()
        and qemu_system_binary(arch) is not None
        and guest_assets_available(arch)
    )
