"""Reading back what ``build`` produced, and refusing to boot what will not boot.

A bundle is the family's boot contract: one ext4 rootfs, one kernel, one
initramfs, and a manifest naming them, under
``CACHE/microvm/<service_id>/<arch>/``. There is exactly one of them per service
and architecture, and both hypervisors boot the same one -- QEMU under TCG boots
the bundle CH's builder wrote, which is why there is one build cache and not two.

The three checks here run before a process is started, because each of them turns
a guest that boots and then hangs into one precise error at launch time: a missing
image, an initramfs whose ``/init`` speaks a different contract version than this
checkout, an entrypoint the guest would never find.
"""
import json
from pathlib import Path
from typing import Dict

from protos import celaut_pb2 as celaut
from src.virtualizers.architecture import UnsupportedArchitectureException, get_arch_tag
from src.virtualizers.entry_path import resolve_entrypoint_path
from src.virtualizers.microvm import initramfs as microvm_initramfs
from src.virtualizers.microvm import paths
from src.virtualizers.microvm.errors import MicroVMError


def resolve_service_arch(service_id: str, service: celaut.Service) -> str:
    """The guest architecture to boot this service as.

    The manifest answers it whenever it says anything at all. The fallback is the
    disk: a service built here has exactly one bundle unless it was built for two
    architectures, and a single candidate is not a guess. Anything else raises,
    because booting the wrong architecture is a guest that panics with nothing
    useful on its console.
    """
    arch = get_arch_tag(service=service, metadata=None)
    if arch:
        return arch

    base_dir = paths.optional_family_root()
    base_dir = (base_dir / service_id) if base_dir else None
    if base_dir and base_dir.is_dir():
        candidates = [p.name for p in base_dir.iterdir() if p.is_dir() and (p / "bundle.json").is_file()]
        if len(candidates) == 1:
            return candidates[0]

    raise UnsupportedArchitectureException(arch="unknown")


def load_bundle(service_id: str, arch: str) -> Dict[str, str]:
    bundle_dir = paths.bundle_dir(service_id, arch)
    bundle_path = bundle_dir / "bundle.json"
    if not bundle_path.is_file():
        raise MicroVMError(f"Missing microVM bundle manifest: {bundle_path}")

    with open(bundle_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    rootfs_path = Path(bundle.get("rootfs_path", ""))
    kernel_path = Path(bundle.get("kernel_path", ""))
    initramfs_path = Path(bundle.get("initramfs_path", ""))

    if not rootfs_path.is_file():
        raise MicroVMError(f"Missing microVM rootfs image: {rootfs_path}")
    if not kernel_path.is_file():
        raise MicroVMError(f"Missing microVM kernel image: {kernel_path}")
    if not initramfs_path.is_file():
        raise MicroVMError(f"Missing microVM initramfs image: {initramfs_path}")

    return {
        "rootfs_path": str(rootfs_path),
        "kernel_path": str(kernel_path),
        "initramfs_path": str(initramfs_path),
        "arch": bundle.get("arch", arch),
    }


def validate_custom_initramfs(initramfs_path: str) -> None:
    try:
        entries, version = microvm_initramfs.read(initramfs_path)
    except microvm_initramfs.InitramfsReadError as e:
        raise MicroVMError(
            f"Unable to inspect microVM initramfs {initramfs_path}: {e}"
        ) from e

    missing = microvm_initramfs.missing_entries(entries)
    if missing:
        raise MicroVMError(
            "Invalid microVM initramfs. Missing required custom entries: "
            f"{missing}. initramfs={initramfs_path}. Re-run installation to regenerate "
            "the custom initramfs."
        )

    # The image is pinned by digest, while /init's half of its contract with this
    # module lives in the code, so the two can be bumped out of step. Checking the
    # version turns that skew into one precise error here, instead of a guest that
    # boots and then parks forever in /init's fatal() loop while the launch times
    # out with nothing useful to show.
    if version != microvm_initramfs.CONTRACT_VERSION:
        raise MicroVMError(
            "microVM initramfs speaks contract version "
            f"'{version or '<unknown>'}', but this node needs "
            f"'{microvm_initramfs.CONTRACT_VERSION}'. initramfs={initramfs_path}. The "
            "pinned guest asset and this checkout disagree: re-run installation to "
            "fetch the initramfs matching this code."
        )


def validate_entrypoint_strict(service: celaut.Service) -> str:
    try:
        return resolve_entrypoint_path(entry_path=service.container.init.entry_path)
    except ValueError as e:
        raise MicroVMError(f"Invalid microVM entrypoint: {e}") from e
