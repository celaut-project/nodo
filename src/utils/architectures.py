"""
Supported-architecture tables, derived from config.

These lists used to live in src/utils/runtime.py, which is Docker-specific and
imports the `docker` Python library. They are needed on the Cloud-Hypervisor
execution path (via src/virtualizers/architecture.py), so they live here in a
Docker-free module instead — nothing on the CH path imports Docker.

Each entry is a list of architecture aliases; the FIRST element is the
canonical form (e.g. "linux/amd64").
"""
from src.utils.config import ConfigManager

config = ConfigManager()

# Architectures this node can BUILD/pack for. Retained for completeness; packing
# itself is now delegated to the external packer-service.
PACKER_SUPPORTED_ARCHITECTURES = []
if config.get("packer.ARM_PACKER_SUPPORT"):
    PACKER_SUPPORTED_ARCHITECTURES.append(["linux/arm64", "arm64", "arm_64", "aarch64"])
if config.get("packer.X86_PACKER_SUPPORT"):
    PACKER_SUPPORTED_ARCHITECTURES.append(["linux/amd64", "x86_64", "amd64"])

# Alias tables for each executable architecture (canonical form first).
_ARM64_ALIASES = ["linux/arm64", "arm64", "arm_64", "aarch64"]
_AMD64_ALIASES = ["linux/amd64", "x86_64", "amd64"]

# Architectures this node can RUN.
#
# Native arches run under Cloud Hypervisor (KVM) and are gated by the builder
# flags, which the installer derives from `uname -m`. On top of that, when QEMU
# emulation is enabled AND the emulator + guest kernel/initramfs for a foreign
# arch are present, the node ALSO advertises that arch as executable -- it can
# boot it under TCG (celaut-project/nodo#271 makes the cross-arch guest kernel
# available). This is opt-in: with `virtualizers.qemu.ENABLE` false (the default)
# the emulated arches are never added, so a node advertises exactly what it did
# before.
SUPPORTED_ARCHITECTURES = []
if config.get("builder.ARM_SUPPORT"):
    SUPPORTED_ARCHITECTURES.append(list(_ARM64_ALIASES))
if config.get("builder.X86_SUPPORT"):
    SUPPORTED_ARCHITECTURES.append(list(_AMD64_ALIASES))


def _emulated_architectures():
    """Alias lists for foreign arches this node can execute under QEMU/TCG.

    Empty unless emulation is enabled and, per arch, the emulator binary and a
    guest kernel/initramfs are actually present -- so the node never claims an
    emulated arch it cannot boot. Imported lazily to avoid a config import cycle.
    """
    try:
        from src.virtualizers.qemu.config import emulation_ready
    except Exception:
        return []

    already = {arch_list[0] for arch_list in SUPPORTED_ARCHITECTURES}
    emulated = []
    for canonical, aliases in (("linux/arm64", _ARM64_ALIASES), ("linux/amd64", _AMD64_ALIASES)):
        if canonical in already:
            continue
        try:
            if emulation_ready(canonical):
                emulated.append(list(aliases))
        except Exception:
            continue
    return emulated


SUPPORTED_ARCHITECTURES.extend(_emulated_architectures())
