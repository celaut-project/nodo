import json
import math
import os
import stat
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from bee_rpc.client import copy_block_if_exists

from protos import celaut_pb2
from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger
from src.utils.verify import get_service_hex_main_hash
from src.virtualizers.architecture import get_arch_tag, UnsupportedArchitectureException

env_manager = ConfigManager()

CACHE = env_manager.get("CACHE")
KERNEL_PATHS = env_manager.get("virtualizers.cloud_hypervisor.KERNEL_PATHS") or {}
INITRAMFS_PATHS = env_manager.get("virtualizers.cloud_hypervisor.INITRAMFS_PATHS") or {}

OVERHEAD_BYTES = 64 * 1024 * 1024
MIN_ROOTFS_BYTES = 128 * 1024 * 1024
BLOCK_SIZE = 4096
MKFS_MAX_ATTEMPTS = 3
MKFS_GROWTH_FACTOR = 2
_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def _bundle_dir(service_id: str, arch: str) -> Path:
    if not CACHE:
        raise RuntimeError("CACHE path is not configured.")
    return Path(CACHE) / "cloud_hypervisor" / service_id / arch


def _validate_guest_assets(arch: str) -> tuple[str, str]:
    kernel_path = KERNEL_PATHS.get(arch)
    initramfs_path = INITRAMFS_PATHS.get(arch)

    if not kernel_path:
        available = ", ".join(sorted(KERNEL_PATHS.keys())) or "none"
        raise FileNotFoundError(
            f"No kernel path configured for arch '{arch}'. "
            f"Available KERNEL_PATHS: {available}."
        )
    if not os.path.isfile(kernel_path):
        raise FileNotFoundError(
            f"Cloud Hypervisor kernel not found at '{kernel_path}'."
        )

    if not initramfs_path:
        available = ", ".join(sorted(INITRAMFS_PATHS.keys())) or "none"
        raise FileNotFoundError(
            f"No initramfs path configured for arch '{arch}'. "
            f"Available INITRAMFS_PATHS: {available}."
        )
    if not os.path.isfile(initramfs_path):
        raise FileNotFoundError(
            f"Cloud Hypervisor initramfs not found at '{initramfs_path}'."
        )

    return kernel_path, initramfs_path


def _write_item(
    branch: celaut_pb2.Service.Container.Filesystem.ItemBranch,
    base_dir: Path,
    symlinks: List[celaut_pb2.Service.Container.Filesystem.ItemBranch.Link],
) -> None:
    if branch.HasField("filesystem"):
        target_dir = base_dir / branch.name
        target_dir.mkdir(parents=True, exist_ok=True)
        _write_fs(branch.filesystem, target_dir, symlinks)
        return

    if branch.HasField("file"):
        target_path = base_dir / branch.name
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not copy_block_if_exists(buffer=branch.file, directory=str(target_path)):
            with open(target_path, "wb") as f:
                f.write(branch.file)
        return

    if branch.HasField("link"):
        symlinks.append(branch.link)


def _write_fs(
    fs_element: celaut_pb2.Service.Container.Filesystem,
    base_dir: Path,
    symlinks: List[celaut_pb2.Service.Container.Filesystem.ItemBranch.Link],
) -> None:
    for branch in fs_element.branch:
        _write_item(branch, base_dir, symlinks)


def _apply_symlinks(
    symlinks: List[celaut_pb2.Service.Container.Filesystem.ItemBranch.Link],
    root_dir: Path,
) -> None:
    for link in symlinks:
        if not link.dst:
            continue
        dst_path = root_dir / link.dst.lstrip("/")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dst_path.exists() or dst_path.is_symlink():
                dst_path.unlink()
        except FileNotFoundError:
            pass
        os.symlink(link.src, dst_path)


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            file_path = Path(root) / name
            try:
                total += file_path.stat().st_size
            except FileNotFoundError:
                continue
    return total


def _looks_executable_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            header = f.read(4)
    except OSError:
        return False
    return header.startswith(b"#!") or header == b"\x7fELF"


def _chmod_add_exec(path: Path) -> None:
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError:
        return
    if not stat.S_ISREG(mode):
        return
    if mode & _EXEC_BITS:
        return
    os.chmod(path, mode | _EXEC_BITS, follow_symlinks=False)


def _apply_executable_permissions(rootfs_dir: Path, entrypoint: Optional[str]) -> None:
    entrypoint_host_path: Optional[Path] = None
    if entrypoint and entrypoint.startswith("/"):
        entrypoint_host_path = rootfs_dir / entrypoint.lstrip("/")

    for root, _, files in os.walk(rootfs_dir):
        root_path = Path(root)
        for filename in files:
            file_path = root_path / filename
            if file_path.is_symlink():
                continue
            if entrypoint_host_path and file_path == entrypoint_host_path:
                _chmod_add_exec(file_path)
                continue
            if _looks_executable_file(file_path):
                _chmod_add_exec(file_path)


def _is_mkfs_out_of_space_error(stderr: str, stdout: str) -> bool:
    output = f"{stderr}\n{stdout}".lower()
    return (
        "could not allocate block in ext2 filesystem" in output
        or "no space left on device" in output
    )


def _mkfs_ext4(rootfs_dir: Path, image_path: Path, size_bytes: int) -> int:
    current_size = size_bytes

    for attempt in range(1, MKFS_MAX_ATTEMPTS + 1):
        blocks = math.ceil(current_size / BLOCK_SIZE)
        try:
            subprocess.run(
                [
                    "mkfs.ext4",
                    "-b",
                    str(BLOCK_SIZE),
                    "-m",
                    "0",
                    "-d",
                    str(rootfs_dir),
                    str(image_path),
                    str(blocks),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return current_size
        except FileNotFoundError as e:
            raise RuntimeError("mkfs.ext4 not found in PATH.") from e
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.strip() if e.stderr else ""
            stdout = e.stdout.strip() if e.stdout else ""
            if (
                attempt < MKFS_MAX_ATTEMPTS
                and _is_mkfs_out_of_space_error(stderr=stderr, stdout=stdout)
            ):
                current_size = int(current_size * MKFS_GROWTH_FACTOR)
                logger(
                    "mkfs.ext4 ran out of space while populating rootfs. "
                    f"Retrying with larger image ({current_size} bytes)."
                )
                try:
                    if image_path.exists():
                        image_path.unlink()
                except OSError:
                    pass
                continue

            raise RuntimeError(
                f"mkfs.ext4 failed: {stderr or stdout or 'unknown error'}"
            ) from e

    raise RuntimeError("mkfs.ext4 failed after exhausting retries.")


def _is_service_built_for_arch(service_hash: str, arch: str) -> bool:
    if not CACHE:
        return False
    bundle_dir = Path(CACHE) / "cloud_hypervisor" / service_hash / arch
    return (bundle_dir / "rootfs.ext4").is_file() and (bundle_dir / "bundle.json").is_file()


def is_service_built(service_hash: str) -> bool:
    if not CACHE:
        return False
    base_dir = Path(CACHE) / "cloud_hypervisor" / service_hash
    if not base_dir.exists() or not base_dir.is_dir():
        return False

    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        if (entry / "rootfs.ext4").is_file() and (entry / "bundle.json").is_file():
            return True
    return False


def build(
    service: celaut_pb2.Service,
    metadata: celaut_pb2.Metadata,
    service_id: Optional[str] = None,
) -> str:
    if not service_id:
        service_id = get_service_hex_main_hash(metadata=metadata)

    arch = get_arch_tag(service=service, metadata=metadata)
    if not arch:
        raise UnsupportedArchitectureException(arch=str(metadata))

    if _is_service_built_for_arch(service_id, arch):
        return service_id

    kernel_path, initramfs_path = _validate_guest_assets(arch)

    bundle_dir = _bundle_dir(service_id, arch)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    rootfs_dir = bundle_dir / "_rootfs"
    if rootfs_dir.exists():
        shutil.rmtree(rootfs_dir, ignore_errors=True)
    rootfs_dir.mkdir(parents=True, exist_ok=True)

    fs = celaut_pb2.Service.Container.Filesystem()
    fs.ParseFromString(service.container.filesystem)

    symlinks: List[celaut_pb2.Service.Container.Filesystem.ItemBranch.Link] = []
    _write_fs(fs, rootfs_dir, symlinks)
    _apply_symlinks(symlinks, rootfs_dir)
    entrypoint = service.container.entrypoint[0] if len(service.container.entrypoint) == 1 else None
    _apply_executable_permissions(rootfs_dir=rootfs_dir, entrypoint=entrypoint)

    total_bytes = _dir_size_bytes(rootfs_dir)
    initial_size_bytes = max(MIN_ROOTFS_BYTES, total_bytes + OVERHEAD_BYTES)

    rootfs_path = bundle_dir / "rootfs.ext4"
    if rootfs_path.exists():
        rootfs_path.unlink()

    try:
        size_bytes = _mkfs_ext4(rootfs_dir, rootfs_path, initial_size_bytes)
    finally:
        shutil.rmtree(rootfs_dir, ignore_errors=True)

    bundle = {
        "service_id": service_id,
        "arch": arch,
        "rootfs_path": str(rootfs_path),
        "kernel_path": kernel_path,
        "initramfs_path": initramfs_path,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rootfs_size_bytes": size_bytes,
    }

    bundle_path = bundle_dir / "bundle.json"
    with open(bundle_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, sort_keys=True)

    logger(f"Cloud Hypervisor build completed for {service_id} ({arch}).")
    return service_id
