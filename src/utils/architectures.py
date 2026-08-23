"""Supported-architecture tables, derived from what this node can actually run.

These lists used to live in src/utils/runtime.py, which is Docker-specific and
imports the `docker` Python library. They are needed on the Cloud-Hypervisor
execution path (via src/virtualizers/architecture.py), so they live here in a
Docker-free module instead — nothing on the CH path imports Docker.

Each entry is a list of architecture aliases; the FIRST element is the
canonical form (e.g. "linux/amd64").

There is deliberately no config flag saying which architectures the node
executes. There used to be one pair (`builder.ARM_SUPPORT` / `X86_SUPPORT`) and
it could disagree with reality in both directions: set to true on a host that
cannot run that arch, a service was accepted and then died deep inside the CH
build looking for a guest kernel that was never installed; set to false on a host
that could, the node hid capacity it had. Capability is now *derived*:

* the host's own architecture, which Cloud Hypervisor runs under KVM;
* plus every foreign architecture QEMU can emulate here, which
  ``virtualizers.qemu`` answers by checking the emulator binary and the guest
  kernel/initramfs on disk.

So the node advertises exactly what it can boot, and the only way to change that
is to change what is installed (or turn ``virtualizers.qemu.ENABLE`` off).
"""
from src.utils.arch_guard import host_arch_tag
from src.utils.config import ConfigManager

config = ConfigManager()

# Alias tables for each executable architecture (canonical form first).
_ARM64_ALIASES = ["linux/arm64", "arm64", "arm_64", "aarch64"]
_AMD64_ALIASES = ["linux/amd64", "x86_64", "amd64"]

_ALIASES_BY_CANONICAL = {
    "linux/arm64": _ARM64_ALIASES,
    "linux/amd64": _AMD64_ALIASES,
}

# Architectures this node can BUILD/pack for. Retained for completeness; packing
# itself is now delegated to the external packer-service. Unlike execution these
# stay explicit flags: they say what this operator *wants* the packer to accept,
# not what the host is able to boot.
PACKER_SUPPORTED_ARCHITECTURES = []
if config.get("packer.ARM_PACKER_SUPPORT"):
    PACKER_SUPPORTED_ARCHITECTURES.append(list(_ARM64_ALIASES))
if config.get("packer.X86_PACKER_SUPPORT"):
    PACKER_SUPPORTED_ARCHITECTURES.append(list(_AMD64_ALIASES))


def resolve_supported_architectures(host_arch, emulation_ready):
    """The architectures a node can execute, given its host arch and an emulation
    probe. Pure, so what the node advertises is testable without an install.

    ``host_arch`` is a canonical tag (or None when the host reports a machine type
    nodo has no table for, in which case nothing native is advertised rather than
    something wrong). ``emulation_ready`` is called per FOREIGN arch and answers
    whether a guest of it can actually be booted here -- emulation enabled, the
    emulator binary present, guest kernel and initramfs on disk. It is allowed to
    raise: a broken emulation probe costs the foreign arch, never the native one.

    The host's own arch comes first, and is never asked about: Cloud Hypervisor
    boots it under KVM, with nothing optional in the way.
    """
    supported = []

    native_aliases = _ALIASES_BY_CANONICAL.get(host_arch or "")
    if native_aliases:
        supported.append(list(native_aliases))

    native = {entry[0] for entry in supported}
    for canonical, aliases in _ALIASES_BY_CANONICAL.items():
        if canonical in native:
            continue
        try:
            if emulation_ready(canonical):
                supported.append(list(aliases))
        except Exception:
            continue

    return supported


def _emulation_ready(arch):
    """Whether QEMU can boot ``arch`` here. Imported lazily to avoid a config
    import cycle, and never fatal: an unimportable QEMU backend means no emulated
    arch, not a node that cannot decide what it runs."""
    try:
        from src.virtualizers.qemu.config import emulation_ready
    except Exception:
        return False
    return emulation_ready(arch)


# Architectures this node can RUN: native under CH/KVM, plus whatever QEMU can
# emulate here. Resolved once at import, like every other config-derived table --
# installing an emulator or a guest kernel takes effect on the next node start.
SUPPORTED_ARCHITECTURES = resolve_supported_architectures(host_arch_tag(), _emulation_ready)
