import json
import math
import os
import stat
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, List, Set, Tuple
import tempfile

import warnings
from google.protobuf.message import DecodeError
from bee_rpc import buffer_pb2
from bee_rpc.client import copy_block_if_exists, get_hash_from_block

from protos import celaut_pb2
from src.utils.config import ConfigManager
from src.utils.filesystem_xattrs import (
    FilesystemNodeMetadata,
    parse_filesystem_metadata_xattrs,
)
from src.utils.logger import LOGGER as logger
from src.utils.verify import get_service_hex_main_hash
from src.virtualizers.architecture import get_arch_tag, UnsupportedArchitectureException
# Image floors and the sizing they feed. Shared with the pricing side, so a quote is
# computed from the same numbers the image is formatted at (see `limits`).
from src.virtualizers.ch import limits

env_manager = ConfigManager()

CACHE = env_manager.get("CACHE")
KERNEL_PATHS = env_manager.get("virtualizers.ch.KERNEL_PATHS") or {}
INITRAMFS_PATHS = env_manager.get("virtualizers.ch.INITRAMFS_PATHS") or {}
SECURITY_CONFIG = env_manager.get("virtualizers.ch.SECURITY", {}) or {}

BLOCK_SIZE = 4096
MKFS_MAX_ATTEMPTS = 3
_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
_DANGEROUS_MODE_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


PATH_CONFINEMENT = _as_bool(SECURITY_CONFIG.get("PATH_CONFINEMENT"), True)
DEVICE_NODES_POLICY = str(SECURITY_CONFIG.get("DEVICE_NODES_POLICY", "deny")).strip().lower()
DEVICE_NODE_ALLOWLIST_RAW = SECURITY_CONFIG.get("DEVICE_NODE_ALLOWLIST", []) or []
REQUIRE_TRUSTED_SERVICE_FOR_DEVICES = _as_bool(
    SECURITY_CONFIG.get("REQUIRE_TRUSTED_SERVICE_FOR_DEVICES"), True
)
TRUSTED_SERVICE_IDS = {
    str(service_id).strip()
    for service_id in (SECURITY_CONFIG.get("TRUSTED_SERVICE_IDS", []) or [])
    if str(service_id).strip()
}
ROOTFS_BUILD_SANDBOX = str(
    SECURITY_CONFIG.get("ROOTFS_BUILD_SANDBOX", "none")
).strip().lower()


@dataclass(frozen=True)
class _PendingSymlink:
    src: str
    dst: str
    rel_path: str
    metadata: Optional[FilesystemNodeMetadata]


@dataclass(frozen=True)
class _DeviceAllowlistEntry:
    path: str
    is_block: bool
    major: int
    minor: int
    mode: Optional[int]


@dataclass(frozen=True)
class _BuildSecurityContext:
    service_id: str
    path_confinement: bool
    device_nodes_policy: str
    require_trusted_service_for_devices: bool
    service_is_trusted_for_devices: bool
    device_allowlist: Tuple[_DeviceAllowlistEntry, ...]


def _bundle_dir(service_id: str, arch: str) -> Path:
    if not CACHE:
        raise RuntimeError("CACHE path is not configured.")
    return Path(CACHE) / "cloud_hypervisor" / service_id / arch


# Names under CACHE/cloud_hypervisor that are not service bundles: the runtime
# directories of live VMs and the preserved debris of failed launches. A service id
# is a hex hash and cannot collide with either, but a function that deletes trees in
# this directory says so explicitly rather than trusting that.
NON_BUNDLE_CACHE_DIRS = frozenset({"runtime", "failures"})


def remove_built_service(service_id: str) -> int:
    """Delete every architecture bundle built for ``service_id``; return bytes freed.

    A bundle is the rootfs image a guest boots from -- gigabytes for a real service
    -- and nothing ever removed one: `nodo remove` cleared the registry and the
    metadata entry, and the build stayed in the cache until somebody deleted
    __cache__ by hand.

    Removing it while instances of the service are running is safe: ``execute``
    copies the image into each instance's own runtime directory at launch, so a
    running guest does not read the bundle again. The next launch rebuilds it.

    Returns 0 when the service has no bundle here. Raises ValueError for anything
    that is not a single bundle directory -- an empty id (which resolves to the
    whole cache), a traversal, or one of ``NON_BUNDLE_CACHE_DIRS``.
    """
    if not CACHE:
        raise RuntimeError("CACHE path is not configured.")

    bundles_root = (Path(CACHE) / "cloud_hypervisor").resolve()
    target = (bundles_root / service_id).resolve()
    if target.parent != bundles_root or target.name in NON_BUNDLE_CACHE_DIRS:
        raise ValueError(
            f"{service_id!r} does not name a service bundle under {bundles_root}; "
            "refusing to delete it."
        )

    if not target.is_dir():
        logger(f"[CH][{service_id}] no built bundle to remove ({target}).")
        return 0

    freed = _dir_size_bytes(target)
    shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        logger(f"[CH][{service_id}] bundle removal left files behind: {target}")
        return max(0, freed - _dir_size_bytes(target))

    logger(f"[CH][{service_id}] event=remove mode=bundle bundle_removed={target} freed_bytes={freed}")
    return freed


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


def _validate_branch_name(name: str, parent_rel_path: str) -> str:
    if not isinstance(name, str):
        raise RuntimeError(
            f"Invalid branch.name at '{parent_rel_path}': expected string, got {type(name).__name__}."
        )
    if "\x00" in name:
        raise RuntimeError(
            f"Invalid branch.name at '{parent_rel_path}': NUL byte is not allowed."
        )
    if name == "":
        raise RuntimeError(
            f"Invalid branch.name at '{parent_rel_path}': empty names are not allowed."
        )
    if name in {".", ".."}:
        raise RuntimeError(
            f"Invalid branch.name at '{parent_rel_path}': '{name}' is not allowed."
        )
    if "/" in name:
        raise RuntimeError(
            f"Invalid branch.name at '{parent_rel_path}': '/' is not allowed."
        )
    return name


def _normalize_guest_path(path: str, field_name: str, *, allow_root: bool) -> str:
    if not isinstance(path, str):
        raise RuntimeError(f"Invalid {field_name}: expected string, got {type(path).__name__}.")
    if "\x00" in path:
        raise RuntimeError(f"Invalid {field_name}: NUL byte is not allowed.")

    candidate = path
    if candidate == "":
        raise RuntimeError(f"Invalid {field_name}: path cannot be empty.")

    parts: List[str] = []
    for part in candidate.split("/"):
        if part == "":
            continue
        if part in {".", ".."}:
            raise RuntimeError(
                f"Invalid {field_name}: path traversal segment '{part}' is not allowed."
            )
        parts.append(part)

    normalized = "/" + "/".join(parts)
    if normalized == "/" and not allow_root:
        raise RuntimeError(f"Invalid {field_name}: root path '/' is not allowed here.")
    return normalized


def _join_relative_path(parent: str, name: str) -> str:
    clean_parent = _normalize_guest_path(parent, "parent path", allow_root=True)
    clean_name = _validate_branch_name(name, clean_parent)
    if clean_parent == "/":
        return f"/{clean_name}"
    return f"{clean_parent}/{clean_name}"


def _safe_rootfs_path(
    rootfs_dir: Path,
    guest_path: str,
    security_context: _BuildSecurityContext,
    *,
    allow_root: bool = False,
) -> Path:
    normalized_guest_path = _normalize_guest_path(
        guest_path, "guest path", allow_root=allow_root
    )
    rootfs_real = rootfs_dir.resolve(strict=False)
    target = (rootfs_real / normalized_guest_path.lstrip("/")).resolve(strict=False)

    if security_context.path_confinement:
        if target != rootfs_real and rootfs_real not in target.parents:
            raise RuntimeError(
                f"Path escapes rootfs confinement: guest_path='{normalized_guest_path}', "
                f"target='{target}', rootfs='{rootfs_real}'."
            )

    return target


def _parse_int_literal(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"Invalid {field_name}: booleans are not allowed.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            raise RuntimeError(f"Invalid {field_name}: empty value.")
        if text.startswith("0o"):
            return int(text, 8)
        if text.startswith("0x"):
            return int(text, 16)
        if text.startswith("0") and text.isdigit() and len(text) > 1:
            return int(text, 8)
        return int(text, 10)
    raise RuntimeError(
        f"Invalid {field_name}: expected int or string, got {type(value).__name__}."
    )


def _parse_non_negative_int(value: Any, field_name: str) -> int:
    parsed = _parse_int_literal(value, field_name)
    if parsed < 0:
        raise RuntimeError(f"Invalid {field_name}: must be >= 0.")
    return parsed


def _parse_allowlist_mode(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    parsed_mode = _parse_int_literal(value, field_name)
    if parsed_mode < 0:
        raise RuntimeError(f"Invalid {field_name}: mode must be >= 0.")
    if parsed_mode & _DANGEROUS_MODE_BITS:
        raise RuntimeError(
            f"Invalid {field_name}: setuid/setgid/sticky bits are forbidden in device modes."
        )
    return stat.S_IMODE(parsed_mode)


def _parse_device_allowlist(raw_allowlist: Any) -> Tuple[_DeviceAllowlistEntry, ...]:
    if raw_allowlist is None:
        return tuple()
    if not isinstance(raw_allowlist, list):
        raise RuntimeError(
            "Invalid DEVICE_NODE_ALLOWLIST: expected list of entries."
        )

    entries: List[_DeviceAllowlistEntry] = []
    for index, raw_entry in enumerate(raw_allowlist):
        if not isinstance(raw_entry, dict):
            raise RuntimeError(
                f"Invalid DEVICE_NODE_ALLOWLIST[{index}]: expected object/dict."
            )
        field_prefix = f"DEVICE_NODE_ALLOWLIST[{index}]"
        path = _normalize_guest_path(
            str(raw_entry.get("path", "")), f"{field_prefix}.path", allow_root=False
        )
        if not path.startswith("/dev/"):
            raise RuntimeError(
                f"Invalid {field_prefix}.path: device nodes are only allowed under '/dev/'."
            )

        node_type = str(raw_entry.get("type", "")).strip().lower()
        if node_type not in {"char", "block"}:
            raise RuntimeError(
                f"Invalid {field_prefix}.type: expected 'char' or 'block'."
            )
        major = _parse_non_negative_int(raw_entry.get("major"), f"{field_prefix}.major")
        minor = _parse_non_negative_int(raw_entry.get("minor"), f"{field_prefix}.minor")
        mode = _parse_allowlist_mode(raw_entry.get("mode"), f"{field_prefix}.mode")
        entries.append(
            _DeviceAllowlistEntry(
                path=path,
                is_block=node_type == "block",
                major=major,
                minor=minor,
                mode=mode,
            )
        )

    return tuple(entries)


def _is_service_trusted_for_device_nodes(service_id: str) -> bool:
    return service_id in TRUSTED_SERVICE_IDS


def _build_security_context(service_id: str) -> _BuildSecurityContext:
    policy = DEVICE_NODES_POLICY or "deny"
    if policy not in {"deny", "allowlist"}:
        raise RuntimeError(
            f"Invalid DEVICE_NODES_POLICY '{DEVICE_NODES_POLICY}'. Supported values: deny, allowlist."
        )
    allowlist = _parse_device_allowlist(DEVICE_NODE_ALLOWLIST_RAW)
    return _BuildSecurityContext(
        service_id=service_id,
        path_confinement=PATH_CONFINEMENT,
        device_nodes_policy=policy,
        require_trusted_service_for_devices=REQUIRE_TRUSTED_SERVICE_FOR_DEVICES,
        service_is_trusted_for_devices=_is_service_trusted_for_device_nodes(service_id),
        device_allowlist=allowlist,
    )


def _audit_device_node(
    security_context: _BuildSecurityContext,
    rel_path: str,
    metadata: FilesystemNodeMetadata,
    decision: str,
    reason: str,
) -> None:
    node_type = "block" if metadata.device_is_block else "char"
    logger(
        f"[CH][SECURITY][DEVNODE][{security_context.service_id}] {decision} "
        f"path={rel_path} type={node_type} major={metadata.device_major} "
        f"minor={metadata.device_minor} mode={oct(metadata.mode)} reason={reason}"
    )


def _decode_branch_metadata(
    branch: celaut_pb2.Service.Container.Filesystem.ItemBranch,
    rel_path: str,
) -> Optional[FilesystemNodeMetadata]:
    try:
        return parse_filesystem_metadata_xattrs(branch.xattrs)
    except ValueError as e:
        raise RuntimeError(
            f"Invalid filesystem metadata xattrs at '{rel_path}': {e}"
        ) from e


def _ensure_metadata_matches_branch(
    metadata: Optional[FilesystemNodeMetadata],
    branch: celaut_pb2.Service.Container.Filesystem.ItemBranch,
    rel_path: str,
) -> None:
    if metadata is None:
        return

    if metadata.is_device:
        if not branch.HasField("file"):
            raise RuntimeError(
                f"Device metadata at '{rel_path}' requires file branch placeholder."
            )
        return

    if branch.HasField("filesystem") and not stat.S_ISDIR(metadata.mode):
        raise RuntimeError(
            f"Mode/type mismatch at '{rel_path}': branch is directory but mode is {oct(metadata.mode)}."
        )
    if branch.HasField("file") and not stat.S_ISREG(metadata.mode):
        raise RuntimeError(
            f"Mode/type mismatch at '{rel_path}': branch is file but mode is {oct(metadata.mode)}."
        )
    if branch.HasField("link") and not stat.S_ISLNK(metadata.mode):
        raise RuntimeError(
            f"Mode/type mismatch at '{rel_path}': branch is symlink but mode is {oct(metadata.mode)}."
        )


def _authorize_device_node(
    metadata: FilesystemNodeMetadata,
    rel_path: str,
    security_context: _BuildSecurityContext,
) -> None:
    if metadata.mode & _DANGEROUS_MODE_BITS:
        _audit_device_node(
            security_context=security_context,
            rel_path=rel_path,
            metadata=metadata,
            decision="DENY",
            reason="dangerous mode bits (setuid/setgid/sticky) are forbidden",
        )
        raise RuntimeError(
            f"Device node '{rel_path}' uses forbidden mode bits in mode={oct(metadata.mode)}."
        )

    if security_context.device_nodes_policy == "deny":
        _audit_device_node(
            security_context=security_context,
            rel_path=rel_path,
            metadata=metadata,
            decision="DENY",
            reason="device nodes policy is deny",
        )
        raise RuntimeError(
            f"Device node creation denied by policy for '{rel_path}'."
        )

    if security_context.require_trusted_service_for_devices and not security_context.service_is_trusted_for_devices:
        _audit_device_node(
            security_context=security_context,
            rel_path=rel_path,
            metadata=metadata,
            decision="DENY",
            reason="service is not trusted for device node creation",
        )
        raise RuntimeError(
            f"Device node creation denied for untrusted service at '{rel_path}'."
        )

    expected_perm_mode = stat.S_IMODE(metadata.mode)
    for entry in security_context.device_allowlist:
        if entry.path != rel_path:
            continue
        if entry.is_block != metadata.device_is_block:
            continue
        if entry.major != metadata.device_major or entry.minor != metadata.device_minor:
            continue
        if entry.mode is not None and entry.mode != expected_perm_mode:
            continue
        _audit_device_node(
            security_context=security_context,
            rel_path=rel_path,
            metadata=metadata,
            decision="ALLOW",
            reason="matches configured device node allowlist",
        )
        return

    _audit_device_node(
        security_context=security_context,
        rel_path=rel_path,
        metadata=metadata,
        decision="DENY",
        reason="device node is not in allowlist",
    )
    raise RuntimeError(
        f"Device node '{rel_path}' is not allowed by DEVICE_NODE_ALLOWLIST."
    )


def _assert_inode_matches_metadata_type(
    path: Path,
    metadata: FilesystemNodeMetadata,
    rel_path: str,
) -> None:
    try:
        inode_mode = os.lstat(path).st_mode
    except OSError as e:
        raise RuntimeError(f"Failed to lstat '{rel_path}' before metadata apply: {e}") from e

    if stat.S_IFMT(inode_mode) != stat.S_IFMT(metadata.mode):
        raise RuntimeError(
            f"Inode type changed unexpectedly at '{rel_path}': "
            f"inode={oct(inode_mode)} metadata={oct(metadata.mode)}."
        )


def _apply_chown(path: Path, uid: int, gid: int, follow_symlinks: bool, rel_path: str) -> None:
    try:
        current = os.stat(path) if follow_symlinks else os.lstat(path)
    except OSError as e:
        raise RuntimeError(f"Failed to stat '{rel_path}' before chown: {e}") from e

    if current.st_uid == uid and current.st_gid == gid:
        return

    try:
        if follow_symlinks:
            os.chown(path, uid, gid)
            return

        if hasattr(os, "lchown"):
            os.lchown(path, uid, gid)
            return

        supports_follow = getattr(os, "supports_follow_symlinks", set())
        if os.chown in supports_follow:
            os.chown(path, uid, gid, follow_symlinks=False)
            return

        raise RuntimeError(
            "secure no-follow chown is not supported on this platform"
        )
    except OSError as e:
        raise RuntimeError(
            f"Failed to apply ownership to '{rel_path}': uid={uid}, gid={gid}, error={e}"
        ) from e


def _apply_chmod(path: Path, mode: int, rel_path: str) -> None:
    chmod_mode = stat.S_IMODE(mode)
    try:
        current_mode = stat.S_IMODE(os.lstat(path).st_mode)
    except OSError as e:
        raise RuntimeError(f"Failed to stat '{rel_path}' before chmod: {e}") from e

    if current_mode == chmod_mode:
        return

    try:
        os.chmod(path, chmod_mode, follow_symlinks=False)
    except TypeError as e:
        raise RuntimeError(
            f"Failed to apply mode to '{rel_path}': secure no-follow chmod is not supported ({e})."
        ) from e
    except OSError as e:
        raise RuntimeError(
            f"Failed to apply mode to '{rel_path}': mode={oct(mode)}, error={e}"
        ) from e


def _apply_mtime(path: Path, mtime_ns: int, follow_symlinks: bool, rel_path: str) -> None:
    try:
        current = os.stat(path) if follow_symlinks else os.lstat(path)
    except OSError as e:
        raise RuntimeError(f"Failed to stat '{rel_path}' before utime: {e}") from e

    if current.st_mtime_ns == mtime_ns:
        return

    try:
        os.utime(path, ns=(mtime_ns, mtime_ns), follow_symlinks=follow_symlinks)
    except TypeError as e:
        raise RuntimeError(
            "Failed to apply mtime to "
            f"'{rel_path}': follow_symlinks={follow_symlinks} is not supported, error={e}"
        ) from e
    except OSError as e:
        raise RuntimeError(
            f"Failed to apply mtime to '{rel_path}': mtime_ns={mtime_ns}, error={e}"
        ) from e


def _create_device_node(
    path: Path,
    metadata: FilesystemNodeMetadata,
    rel_path: str,
    security_context: _BuildSecurityContext,
) -> None:
    if metadata.device_is_block and not stat.S_ISBLK(metadata.mode):
        raise RuntimeError(
            f"Device metadata mismatch at '{rel_path}': mode={oct(metadata.mode)} is not block device."
        )
    if not metadata.device_is_block and not stat.S_ISCHR(metadata.mode):
        raise RuntimeError(
            f"Device metadata mismatch at '{rel_path}': mode={oct(metadata.mode)} is not char device."
        )

    _authorize_device_node(
        metadata=metadata,
        rel_path=rel_path,
        security_context=security_context,
    )

    dev = os.makedev(metadata.device_major, metadata.device_minor)
    try:
        os.mknod(path, metadata.mode, dev)
    except OSError as e:
        raise RuntimeError(
            "Failed to create device node at "
            f"'{rel_path}': major={metadata.device_major}, minor={metadata.device_minor}, "
            f"is_block={int(metadata.device_is_block)}, error={e}"
        ) from e


def _apply_regular_metadata(path: Path, metadata: FilesystemNodeMetadata, rel_path: str) -> None:
    _assert_inode_matches_metadata_type(path=path, metadata=metadata, rel_path=rel_path)
    _apply_chown(path, metadata.uid, metadata.gid, follow_symlinks=False, rel_path=rel_path)
    _apply_chmod(path, metadata.mode, rel_path=rel_path)
    _apply_mtime(path, metadata.mtime_ns, follow_symlinks=False, rel_path=rel_path)


def _apply_symlink_metadata(path: Path, metadata: FilesystemNodeMetadata, rel_path: str) -> None:
    _assert_inode_matches_metadata_type(path=path, metadata=metadata, rel_path=rel_path)
    _apply_chown(path, metadata.uid, metadata.gid, follow_symlinks=False, rel_path=rel_path)
    _apply_mtime(path, metadata.mtime_ns, follow_symlinks=False, rel_path=rel_path)


def _write_item(
    branch: celaut_pb2.Service.Container.Filesystem.ItemBranch,
    root_dir: Path,
    parent_rel_path: str,
    symlinks: List[_PendingSymlink],
    legacy_regular_files: Set[Path],
    security_context: _BuildSecurityContext,
) -> None:
    rel_path = _join_relative_path(parent_rel_path, branch.name)
    metadata = _decode_branch_metadata(branch=branch, rel_path=rel_path)
    _ensure_metadata_matches_branch(metadata=metadata, branch=branch, rel_path=rel_path)
    target_path = _safe_rootfs_path(
        rootfs_dir=root_dir,
        guest_path=rel_path,
        security_context=security_context,
    )

    if branch.HasField("filesystem"):
        target_path.mkdir(parents=True, exist_ok=True)
        _write_fs(
            fs_element=branch.filesystem,
            root_dir=root_dir,
            parent_rel_path=rel_path,
            symlinks=symlinks,
            legacy_regular_files=legacy_regular_files,
            security_context=security_context,
        )
        if metadata is not None:
            _apply_regular_metadata(path=target_path, metadata=metadata, rel_path=rel_path)
        return

    if branch.HasField("file"):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if metadata is not None and metadata.is_device:
            _create_device_node(
                path=target_path,
                metadata=metadata,
                rel_path=rel_path,
                security_context=security_context,
            )
            _apply_regular_metadata(path=target_path, metadata=metadata, rel_path=rel_path)
            return

        if not copy_block_if_exists(buffer=branch.file, directory=str(target_path)):
            # copy_block_if_exists returns False in two very different cases:
            #   (1) branch.file is genuine inline content (small files)   -> writing it is correct
            #   (2) branch.file is a block *pointer* (large files) whose block copy failed
            #       (missing / partial / unsupported multiblock block in the local registry)
            # Case (2) MUST NOT fall through to the inline write: branch.file is then only the
            # 36-byte Buffer.Block pointer, and writing it as the file silently corrupts large
            # binaries (dockerd -> "line 2: D: command not found"; libpython -> "invalid ELF
            # header"). Distinguish them with the same detection copy_block_if_exists uses, and
            # fail closed on a real block instead of writing garbage.
            _is_block_pointer = False
            block_id = None
            _blk = None
            try:
                _blk = buffer_pb2.Buffer.Block()
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=RuntimeWarning)
                    _blk.ParseFromString(branch.file)
                # Match copy_block_if_exists' own resolution: the block pointers this
                # pipeline produces carry a single hash of type Enviroment.hash_type
                # (internal_block=False), not the empty type (internal_block=True). Check
                # both so the guard actually fires for hash-typed pointers.
                block_id = (
                    get_hash_from_block(block=_blk, internal_block=True)
                    or get_hash_from_block(block=_blk, internal_block=False)
                )
                
                _is_block_pointer = block_id is not None
            except DecodeError:
                _is_block_pointer = False

            if _is_block_pointer:  #  Si llega aqui, copy_block_if_exists tuvo que retornar el último False.

                # Vuelve a obtener ambos hashes para el mensaje de error, para que sea más fácil de depurar.
                internal = get_hash_from_block(_blk, internal_block=True)
                external = get_hash_from_block(_blk, internal_block=False)

                raise RuntimeError(
                    f"""
                Block reconstruction failed.

                block_id={block_id!r}
                type(block_id)={type(block_id)}
                _is_block_pointer={_is_block_pointer}

                internal={internal!r} ({type(internal)})
                external={external!r} ({type(external)})
                block_id={block_id!r}
                
                Parsed block:
                {_blk}

                Raw bytes:
                {branch.file!r}
                """
                )
            
            # At this point, is not a block pointer, so we can write the inline content to the file.
            with open(target_path, "wb") as f:
                f.write(branch.file)
        
        if metadata is not None:
            _apply_regular_metadata(path=target_path, metadata=metadata, rel_path=rel_path)
        else:
            legacy_regular_files.add(target_path)
        return

    if branch.HasField("link"):
        normalized_link_dst = _normalize_guest_path(
            branch.link.dst,
            field_name=f"link.dst at '{rel_path}'",
            allow_root=False,
        )
        if normalized_link_dst != rel_path:
            raise RuntimeError(
                f"Invalid link.dst at '{rel_path}': expected '{rel_path}', got '{normalized_link_dst}'."
            )
        if not isinstance(branch.link.src, str) or not branch.link.src:
            raise RuntimeError(f"Invalid link.src at '{rel_path}': empty source is not allowed.")
        if "\x00" in branch.link.src:
            raise RuntimeError(f"Invalid link.src at '{rel_path}': NUL byte is not allowed.")
        symlinks.append(
            _PendingSymlink(
                src=branch.link.src,
                dst=normalized_link_dst,
                rel_path=rel_path,
                metadata=metadata,
            )
        )
        return

    raise RuntimeError(f"Filesystem branch '{rel_path}' does not define an item.")


def _write_fs(
    fs_element: celaut_pb2.Service.Container.Filesystem,
    root_dir: Path,
    parent_rel_path: str,
    symlinks: List[_PendingSymlink],
    legacy_regular_files: Set[Path],
    security_context: _BuildSecurityContext,
) -> None:
    for branch in fs_element.branch:
        _write_item(
            branch=branch,
            root_dir=root_dir,
            parent_rel_path=parent_rel_path,
            symlinks=symlinks,
            legacy_regular_files=legacy_regular_files,
            security_context=security_context,
        )


def _apply_symlinks(
    symlinks: List[_PendingSymlink],
    root_dir: Path,
    security_context: _BuildSecurityContext,
) -> None:
    for symlink in symlinks:
        if not symlink.dst:
            continue
        dst_path = _safe_rootfs_path(
            rootfs_dir=root_dir,
            guest_path=symlink.dst,
            security_context=security_context,
        )
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dst_path.exists() or dst_path.is_symlink():
                dst_path.unlink()
        except FileNotFoundError:
            pass
        try:
            os.symlink(symlink.src, dst_path)
        except OSError as e:
            raise RuntimeError(
                f"Failed to create symlink '{symlink.rel_path}' -> '{symlink.src}': {e}"
            ) from e
        if symlink.metadata is not None:
            _apply_symlink_metadata(
                path=dst_path, metadata=symlink.metadata, rel_path=symlink.rel_path
            )


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
        mode = os.lstat(path).st_mode
    except OSError:
        return
    if not stat.S_ISREG(mode):
        return
    if mode & _EXEC_BITS:
        return
    try:
        os.chmod(path, mode | _EXEC_BITS, follow_symlinks=False)
    except TypeError as e:
        raise RuntimeError(
            f"Failed to set executable bit on '{path}': secure no-follow chmod is not supported ({e})."
        ) from e


def _apply_executable_permissions(
    rootfs_dir: Path,
    entrypoint: Optional[str],
    candidate_files: Optional[Set[Path]] = None,
    force_entrypoint: bool = False,
) -> None:
    entrypoint_host_path: Optional[Path] = None
    if entrypoint and entrypoint.startswith("/"):
        entrypoint_host_path = rootfs_dir / entrypoint.lstrip("/")

    if candidate_files is None:
        files_to_inspect: List[Path] = []
        for root, _, files in os.walk(rootfs_dir):
            root_path = Path(root)
            for filename in files:
                files_to_inspect.append(root_path / filename)
    else:
        files_to_inspect = sorted(candidate_files, key=lambda p: str(p))

    for file_path in files_to_inspect:
        if not file_path.exists() or file_path.is_symlink():
            continue
        if force_entrypoint and entrypoint_host_path and file_path == entrypoint_host_path:
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
                current_size = int(current_size * limits.MKFS_GROWTH_FACTOR)
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


def _read_built_rootfs_size_bytes(bundle_dir: Path) -> Optional[int]:
    bundle_path = bundle_dir / "bundle.json"
    rootfs_path = bundle_dir / "rootfs.ext4"
    if not bundle_path.is_file() or not rootfs_path.is_file():
        return None

    try:
        with open(bundle_path, "r", encoding="utf-8") as f:
            bundle = json.load(f)
        rootfs_size_bytes = int(bundle.get("rootfs_size_bytes") or 0)
        if rootfs_size_bytes > 0:
            return rootfs_size_bytes
    except Exception:
        pass

    try:
        return int(rootfs_path.stat().st_size)
    except Exception:
        return None


def _is_service_built_for_arch(
    service_hash: str,
    arch: str,
    service: Optional[celaut_pb2.Service] = None,
) -> bool:
    if not CACHE:
        return False
    bundle_dir = Path(CACHE) / "cloud_hypervisor" / service_hash / arch
    if not (bundle_dir / "rootfs.ext4").is_file() or not (bundle_dir / "bundle.json").is_file():
        return False

    if service is None:
        return True

    requested_disk_space_bytes = limits.requested_disk_space_bytes(service)
    if not requested_disk_space_bytes:
        return True

    built_rootfs_size_bytes = _read_built_rootfs_size_bytes(bundle_dir)
    if built_rootfs_size_bytes is None:
        return False

    return built_rootfs_size_bytes >= requested_disk_space_bytes


def is_service_built(service_hash: str) -> bool:
    if not CACHE:
        return False
    base_dir = Path(CACHE) / "cloud_hypervisor" / service_hash
    if not base_dir.exists() or not base_dir.is_dir():
        return False

    for rootfs_path in base_dir.rglob("rootfs.ext4"):
        if rootfs_path.is_file() and (rootfs_path.parent / "bundle.json").is_file():
            return True
    return False


def built_rootfs_size_bytes(service_hash: str) -> Optional[int]:
    """Size of the rootfs image an already-built service would hand an instance.

    This is the disk figure the maintenance tick will price the instance by (#262),
    so a quote issued for a built service can be exact instead of a floor. None when
    the service is not built here, which leaves the caller with the floor.

    Takes the largest image across the built architectures rather than resolving
    which one will run: pricing a client above what it will be charged refuses a
    request, pricing it below gives the node's disk away, and only the first of those
    is recoverable.
    """
    if not CACHE:
        return None
    base_dir = Path(CACHE) / "cloud_hypervisor" / service_hash
    if not base_dir.is_dir():
        return None

    sizes = []
    for rootfs_path in base_dir.rglob("rootfs.ext4"):
        if not rootfs_path.is_file():
            continue
        size = _read_built_rootfs_size_bytes(rootfs_path.parent)
        if size:
            sizes.append(size)

    return max(sizes) if sizes else None


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

    if _is_service_built_for_arch(service_id, arch, service=service):
        return service_id

    security_context = _build_security_context(service_id=service_id)
    if not security_context.path_confinement:
        logger(
            f"[CH][SECURITY][{service_id}] WARNING: PATH_CONFINEMENT is disabled."
        )
    if ROOTFS_BUILD_SANDBOX != "none":
        raise RuntimeError(
            f"Unsupported ROOTFS_BUILD_SANDBOX mode '{ROOTFS_BUILD_SANDBOX}'. "
            "Currently supported value is 'none'."
        )

    kernel_path, initramfs_path = _validate_guest_assets(arch)

    bundle_dir = _bundle_dir(service_id, arch)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    rootfs_dir = Path(tempfile.mkdtemp(prefix="_rootfs.", dir=str(bundle_dir)))
    os.chmod(rootfs_dir, 0o700)

    fs = celaut_pb2.Service.Container.Filesystem()
    fs.ParseFromString(service.container.filesystem)

    symlinks: List[_PendingSymlink] = []
    legacy_regular_files: Set[Path] = set()
    _write_fs(
        fs_element=fs,
        root_dir=rootfs_dir,
        parent_rel_path="/",
        symlinks=symlinks,
        legacy_regular_files=legacy_regular_files,
        security_context=security_context,
    )
    _apply_symlinks(symlinks, rootfs_dir, security_context)
    entrypoint = (
        service.container.init.entry_path[0]
        if len(service.container.init.entry_path) == 1
        else None
    )
    legacy_entrypoint = bool(
        entrypoint
        and entrypoint.startswith("/")
        and (rootfs_dir / entrypoint.lstrip("/")) in legacy_regular_files
    )
    _apply_executable_permissions(
        rootfs_dir=rootfs_dir,
        entrypoint=entrypoint,
        candidate_files=legacy_regular_files,
        force_entrypoint=legacy_entrypoint,
    )

    total_bytes = _dir_size_bytes(rootfs_dir)
    requested_disk_space_bytes = limits.requested_disk_space_bytes(service)
    initial_size_bytes = limits.initial_rootfs_size_bytes(
        service=service,
        total_bytes=total_bytes,
    )

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
        "requested_disk_space_bytes": requested_disk_space_bytes,
        "rootfs_size_bytes": size_bytes,
    }

    bundle_path = bundle_dir / "bundle.json"
    with open(bundle_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, sort_keys=True)

    logger(f"Cloud Hypervisor build completed for {service_id} ({arch}).")
    return service_id
