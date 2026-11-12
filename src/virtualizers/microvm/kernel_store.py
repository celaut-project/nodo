"""Guest kernels indexed by hash, so a kernel update never changes a built service.

Before this store, the installer wrote the guest kernel of each architecture to one
fixed path (``kernels/<arch>/vmlinuz``) and every bundle named that path. A
guest-kernel update overwrote the file, so every existing bundle, hibernated ones
included, booted the new kernel at its next start, with no notice (#536).

Now:

* The store is ``<KERNEL_STORE>/<hash>/vmlinuz``. ``<hash>`` is the lowercase hex
  SHA-256 of the file (the algorithm of the pinned release digests, so the installer
  knows the path before it downloads). A file in the store never changes: the
  installer writes a new kernel to a new directory, and nothing rewrites one.
* ``virtualizers.ch.KERNEL_PATHS`` keeps its meaning: the kernel that a NEW build of
  that architecture gets. It can point into the store or anywhere else.
* ``bundle.json`` records ``kernel_hash`` (and ``kernel_hash_algorithm``, the
  celaut identifier of the algorithm: its output for the empty input). A launch
  resolves the hash, never the path. If no kernel with that hash is on the node,
  the launch fails with the hash in the message; it never boots another kernel.
* A bundle written before the store has only ``kernel_path``. It is migrated by
  hashing the file that it names, the first time the node reads it (and by
  ``migrate_all_bundles`` at the node start and in ``nodo migrate``). The installer
  no longer writes the old fixed path, so that file still holds the kernel that
  the bundle was built with.
* ``virtualizers.ch.KERNEL_ABI_ALIASES`` maps the hash of a kernel on the node to
  the hashes of older kernels with the same ABI: the operator's claim that the
  kernel can boot what was built for them. A hash that is not on the node resolves
  to a kernel that claims it.

The daemon may not be able to write the store (``--service-user`` mode: the store
belongs to root). Then the hash is recorded anyway and resolved by content against
the configured kernel files; ``nodo migrate`` (root) copies them into the store.
"""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from src.utils.config import ConfigManager
from src.utils.hashing import SHA256_ID
from src.virtualizers.microvm.errors import MicroVMError

env_manager = ConfigManager()

# The algorithm of the store index and of bundle.json's kernel_hash.
STORE_ALGORITHM_ID = SHA256_ID
STORE_ALGORITHM_HEX = STORE_ALGORITHM_ID.hex()
KERNEL_FILE_NAME = "vmlinuz"
_CHUNK = 1 << 20

# (path, size, mtime_ns, inode) -> hex. A kernel is ~15 MiB; hashing it at every
# launch would be waste, and a file that changes gets a new key.
_HASH_CACHE: Dict[Tuple[str, int, int, int], str] = {}


class KernelNotFound(MicroVMError):
    """No kernel with the hash that a bundle asks for is on this node."""

    def __init__(self, kernel_hash: str, where: str = ""):
        self.kernel_hash = kernel_hash
        suffix = f" ({where})" if where else ""
        super().__init__(
            f"The guest kernel {kernel_hash} that this service was built with is not "
            f"on this node{suffix}. The node never boots it with another kernel: put "
            f"that kernel back in the kernel store, declare a kernel with the same ABI "
            f"in virtualizers.ch.KERNEL_ABI_ALIASES, or build the service again."
        )


def _main_dir() -> str:
    return str(env_manager.get("main.MAIN_DIR") or "/nodo")


def store_root() -> Path:
    configured = env_manager.get("virtualizers.ch.KERNEL_STORE")
    if configured:
        return Path(str(configured))
    return Path(_main_dir()) / "cloud_hypervisor" / "kernels" / "store"


def store_path(kernel_hash: str) -> Path:
    return store_root() / kernel_hash / KERNEL_FILE_NAME


def _valid_hash(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def file_hashes(path, algorithms=("sha256",)) -> Dict[str, str]:
    """Hex digests of a file, one per hashlib name, in one read."""
    hashers = {name: hashlib.new(name) for name in algorithms}
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            for h in hashers.values():
                h.update(chunk)
    return {name: h.hexdigest() for name, h in hashers.items()}


def file_hash(path) -> str:
    """The store hash (SHA-256 hex) of a file, cached by (path, size, mtime, inode)."""
    st = os.stat(path)
    key = (str(path), st.st_size, st.st_mtime_ns, st.st_ino)
    cached = _HASH_CACHE.get(key)
    if cached:
        return cached
    value = file_hashes(path)["sha256"]
    _HASH_CACHE[key] = value
    return value


def ingest(path) -> str:
    """Copy a kernel file into the store (if absent) and return its hash.

    Raises OSError (PermissionError in service-user mode) when the store cannot be
    written; the file itself is never changed.
    """
    kernel_hash = file_hash(path)
    target = store_path(kernel_hash)
    if target.is_file():
        return kernel_hash
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ingest.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
            shutil.copyfileobj(src, out, _CHUNK)
        if file_hashes(tmp)["sha256"] != kernel_hash:
            raise OSError(f"The kernel {path} changed while it was copied to the store.")
        os.chmod(tmp, 0o444)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return kernel_hash


def try_ingest(path) -> str:
    """:func:`ingest`, but a store that this process cannot write is not an error."""
    try:
        return ingest(path)
    except OSError:
        return file_hash(path)


def abi_aliases() -> Dict[str, List[str]]:
    """``{kernel hash on the node: [older hashes it claims the ABI of]}`` (valid only)."""
    raw = env_manager.get("virtualizers.ch.KERNEL_ABI_ALIASES") or {}
    out: Dict[str, List[str]] = {}
    if not isinstance(raw, dict):
        return out
    for new, olds in raw.items():
        new = str(new).lower()
        if not _valid_hash(new):
            continue
        if isinstance(olds, str):
            olds = [olds]
        claims = [str(o).lower() for o in (olds or []) if _valid_hash(str(o).lower())]
        if claims:
            out[new] = claims
    return out


def _candidate_files() -> Iterable[str]:
    """Kernel files outside the store that a hash may still name: the configured
    ones, and the fixed paths that installers before the store wrote."""
    seen = set()
    for key in ("virtualizers.ch.KERNEL_PATHS", "virtualizers.qemu.KERNEL_PATHS"):
        for value in (env_manager.get(key) or {}).values():
            if value and value not in seen:
                seen.add(value)
                yield str(value)
    legacy = Path(_main_dir()) / "cloud_hypervisor" / "kernels"
    for arch_dir in ("linux/amd64", "linux/arm64"):
        value = str(legacy / arch_dir / KERNEL_FILE_NAME)
        if value not in seen:
            seen.add(value)
            yield value


def _find_exact(kernel_hash: str) -> Optional[Path]:
    target = store_path(kernel_hash)
    if target.is_file():
        return target
    for candidate in _candidate_files():
        try:
            if os.path.isfile(candidate) and file_hash(candidate) == kernel_hash:
                return Path(candidate)
        except OSError:
            continue
    return None


def resolve(kernel_hash: str) -> Path:
    """The file of the kernel with this hash, or of a kernel that claims its ABI."""
    kernel_hash = str(kernel_hash or "").lower()
    if not _valid_hash(kernel_hash):
        raise KernelNotFound(kernel_hash or "<none>", "not a valid hash")
    found = _find_exact(kernel_hash)
    if found:
        return found
    for new, olds in abi_aliases().items():
        if kernel_hash in olds:
            found = _find_exact(new)
            if found:
                return found
    raise KernelNotFound(kernel_hash)


def kernel_for_build(path) -> Tuple[str, str]:
    """``(hash, path to boot)`` for the configured kernel of a new build."""
    kernel_hash = try_ingest(path)
    target = store_path(kernel_hash)
    return kernel_hash, str(target if target.is_file() else path)


def stamp(bundle: dict) -> bool:
    """Add ``kernel_hash`` to a bundle dict that has only ``kernel_path``.

    True when it changed the dict. A missing kernel file leaves it as it is: the
    launch then fails on the missing file, as before.
    """
    if _valid_hash(str(bundle.get("kernel_hash") or "")):
        return False
    kernel_path = bundle.get("kernel_path")
    if not kernel_path or not os.path.isfile(kernel_path):
        return False
    bundle["kernel_hash"] = try_ingest(kernel_path)
    bundle["kernel_hash_algorithm"] = STORE_ALGORITHM_HEX
    target = store_path(bundle["kernel_hash"])
    if target.is_file():
        bundle["kernel_path"] = str(target)
    return True


def _write_in_place(path: Path, bundle: dict) -> None:
    # In place, not a rename: the file keeps its owner, so a root `nodo migrate`
    # does not take a bundle away from the service user.
    with open(path, "r+", encoding="utf-8") as f:
        f.seek(0)
        json.dump(bundle, f, indent=2, sort_keys=True)
        f.truncate()


def migrate_bundle_file(bundle_path: Path) -> bool:
    """Stamp one ``bundle.json`` in place. True when it was rewritten."""
    try:
        with open(bundle_path, "r", encoding="utf-8") as f:
            bundle = json.load(f)
    except (OSError, ValueError):
        return False
    if not stamp(bundle):
        return False
    try:
        _write_in_place(bundle_path, bundle)
    except OSError:
        return False
    return True


def iter_bundle_files() -> Iterable[Path]:
    from src.virtualizers.microvm import paths

    root = paths.optional_family_root()
    if not root or not root.is_dir():
        return
    for service_dir in sorted(root.iterdir()):
        if service_dir.name in paths.NON_BUNDLE_ENTRIES or not service_dir.is_dir():
            continue
        for arch_dir in sorted(service_dir.iterdir()):
            candidate = arch_dir / "bundle.json"
            if candidate.is_file():
                yield candidate


def migrate_all_bundles() -> Dict[str, int]:
    """Stamp every bundle that has no kernel hash; ingest configured kernels.

    Safe to run any number of times. Returns counts for a log line.
    """
    counts = {"bundles": 0, "migrated": 0, "kernels_ingested": 0}
    for candidate in list(_candidate_files()):
        if os.path.isfile(candidate):
            before = store_path(file_hash(candidate)).is_file()
            try:
                ingest(candidate)
                if not before:
                    counts["kernels_ingested"] += 1
            except OSError:
                pass
    for bundle_path in iter_bundle_files():
        counts["bundles"] += 1
        if migrate_bundle_file(bundle_path):
            counts["migrated"] += 1
    return counts


def store_listing() -> List[Dict[str, object]]:
    """The kernels in the store: hash, size and the hashes that it claims."""
    root = store_root()
    aliases = abi_aliases()
    out: List[Dict[str, object]] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        kernel = entry / KERNEL_FILE_NAME
        if not (_valid_hash(entry.name) and kernel.is_file()):
            continue
        out.append({
            "hash": entry.name,
            "size": kernel.stat().st_size,
            "path": str(kernel),
            "abi_aliases": aliases.get(entry.name, []),
        })
    return out


def bundle_kernel_report() -> Dict[str, object]:
    """For doctor: bundles per kernel hash, and the ones that cannot boot."""
    by_hash: Dict[str, int] = {}
    unstamped: List[str] = []
    unresolved: Dict[str, List[str]] = {}
    for bundle_path in iter_bundle_files():
        try:
            with open(bundle_path, "r", encoding="utf-8") as f:
                bundle = json.load(f)
        except (OSError, ValueError):
            continue
        name = f"{bundle_path.parent.parent.name[:16]}/{bundle_path.parent.name}"
        kernel_hash = str(bundle.get("kernel_hash") or "")
        if not _valid_hash(kernel_hash):
            unstamped.append(name)
            continue
        by_hash[kernel_hash] = by_hash.get(kernel_hash, 0) + 1
        try:
            resolve(kernel_hash)
        except KernelNotFound:
            unresolved.setdefault(kernel_hash, []).append(name)
    return {"by_hash": by_hash, "unstamped": unstamped, "unresolved": unresolved}
