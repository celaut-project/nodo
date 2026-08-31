"""Architecture tags, and whether this host can BUILD for a foreign one.

The build side mirrors the philosophy the execution side already adopted in
``src/utils/architectures.py``: capability is *derived from what is installed*,
never declared. A node that can boot a foreign-arch guest advertises it; a node
that cannot, does not. This module does the same thing one layer down, for the
packer's BuildKit step.

Cross-arch builds work exactly when the kernel has a ``binfmt_misc`` handler
registered for the target architecture and that handler is enabled -- that is
what lets a ``RUN`` line in an ``arm64`` build execute on an ``amd64`` host.
BuildKit needs nothing else: it already passes ``--opt platform=<target>`` and
resolves the foreign base image from the registry, so the only question that has
ever mattered is whether the emulator is wired into the kernel.

``ensure_native_arch`` used to answer that question with an unconditional raise
whose message asserted "QEMU/binfmt support was removed". On a host where the
handler *is* registered that statement is simply false, and the guard rejected
builds the host completes correctly. It now probes ``/proc/sys/fs/binfmt_misc``
and only refuses when emulation really is unavailable -- and says which handler
is missing, so the failure is actionable.
"""
import os
import platform
from typing import Optional


ARCH_ALIASES = {
    "linux/amd64": "linux/amd64",
    "amd64": "linux/amd64",
    "x86_64": "linux/amd64",
    "linux/arm64": "linux/arm64",
    "arm64": "linux/arm64",
    "arm_64": "linux/arm64",
    "aarch64": "linux/arm64",
}

BINFMT_MISC_DIR = "/proc/sys/fs/binfmt_misc"

# Canonical arch tag -> the binfmt_misc handler names that can run its binaries.
# More than one name is accepted because the same emulator is registered under
# different names by different provisioners (``qemu-user-static``, the
# ``tonistiigi/binfmt`` image, a hand-rolled ``register`` write).
BINFMT_HANDLERS = {
    "linux/amd64": ("qemu-x86_64", "qemu-x86_64-static", "x86_64"),
    "linux/arm64": ("qemu-aarch64", "qemu-aarch64-static", "aarch64"),
}


def normalize_arch_tag(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).strip().lower()
    return ARCH_ALIASES.get(normalized)


def host_arch_tag() -> Optional[str]:
    return normalize_arch_tag(platform.machine())


def _binfmt_handler_enabled(name: str) -> bool:
    """Whether ``binfmt_misc`` handler ``name`` exists and is enabled.

    A registration can be present but disabled (``echo 0 > .../<name>``), in
    which case the kernel will not use it and a cross-arch ``RUN`` still fails.
    The handler file's first line is ``enabled`` or ``disabled``, so read it
    rather than trusting the file's existence. Any read error means "cannot
    confirm", which is treated as not available -- an unreadable proc entry must
    never be the reason a build is *allowed* to start and then die deep inside
    BuildKit.
    """
    path = os.path.join(BINFMT_MISC_DIR, name)
    try:
        with open(path, "r") as handler:
            return handler.readline().strip() == "enabled"
    except OSError:
        return False


def emulation_available(target_arch: Optional[str]) -> bool:
    """Whether binaries of ``target_arch`` can be executed on this host.

    True for the host's own architecture (nothing to emulate) and for a foreign
    architecture with an enabled ``binfmt_misc`` handler. Unknown tags answer
    False: this is the permissive-side helper, and nothing should be allowed on
    the strength of an arch nodo has no table for.
    """
    normalized_target = normalize_arch_tag(target_arch)
    if not normalized_target:
        return False

    if normalized_target == host_arch_tag():
        return True

    return any(
        _binfmt_handler_enabled(name)
        for name in BINFMT_HANDLERS.get(normalized_target, ())
    )


def ensure_native_arch(target_arch: Optional[str], context: str = "build") -> None:
    """Raise unless ``target_arch`` can be built here.

    Kept under its original name so every call site (and anything vendored from
    this file, such as the packer-service) keeps working; the *rule* is what
    changed. A native target passes as before. A foreign target now passes when
    the kernel can emulate it, and is refused -- naming the handler it wants --
    when it cannot. An unrecognized tag is still ignored, leaving that decision
    to the caller that knows the service manifest.
    """
    normalized_target = normalize_arch_tag(target_arch)
    normalized_host = host_arch_tag()

    if not normalized_target or not normalized_host:
        return

    if normalized_target == normalized_host:
        return

    if emulation_available(normalized_target):
        return

    wanted = " or ".join(BINFMT_HANDLERS.get(normalized_target, ()))
    raise RuntimeError(
        f"{context}: cross-architecture builds need a binfmt_misc handler for the target, "
        f"and none is enabled here. Host={normalized_host}, target={normalized_target}. "
        f"Register one (for example `docker run --privileged --rm tonistiigi/binfmt --install "
        f"{normalized_target.split('/')[-1]}`, or install qemu-user-static) so that "
        f"{BINFMT_MISC_DIR}/{{{wanted}}} exists and reads `enabled` -- or build on a host "
        f"whose architecture matches the target."
    )
