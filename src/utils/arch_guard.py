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


def normalize_arch_tag(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).strip().lower()
    return ARCH_ALIASES.get(normalized)


def host_arch_tag() -> Optional[str]:
    return normalize_arch_tag(platform.machine())


def ensure_native_arch(target_arch: Optional[str], context: str = "build") -> None:
    normalized_target = normalize_arch_tag(target_arch)
    normalized_host = host_arch_tag()

    if not normalized_target or not normalized_host:
        return

    if normalized_target != normalized_host:
        raise RuntimeError(
            f"{context}: cross-architecture builds are disabled because QEMU/binfmt support was removed. "
            f"Host={normalized_host}, target={normalized_target}. Use a host that matches the target architecture."
        )
