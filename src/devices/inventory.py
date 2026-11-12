"""The devices that this node gives: config, host checks, kernels, quarantine (#536).

EXPERIMENTAL and off by default (``devices.ENABLED: false``). The operator:

1. binds each device to ``vfio-pci`` (nodo never unbinds a host driver);
2. lists its PCI address in ``devices.ALLOW``;
3. gives its chip a price in ``devices.BY_CHIP.<pci:vendor:device>.mu_per_second``.

The chip is read from sysfs (:mod:`src.devices.host`), never from the config. A
device is offered when every host check passes, its chip has a price, and it is not
quarantined. Each offered device is one ``Peer.devices`` entry per offered kernel
(``devices.KERNELS``, hashes in the kernel store; empty = the kernel of new builds
for the host architecture). The tags of an entry are that chip and kernel, once per
hash algorithm (SHA-256 and SHA3-256), plus the older SHA-256 hashes that the kernel
claims in ``virtualizers.ch.KERNEL_ABI_ALIASES``.

A device whose reset fails, or that stops answering, is quarantined: the node stops
offering it until the operator clears it with ``nodo devices clear``.
"""
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from protos import celaut_pb2 as celaut
from src.devices import host, spec
from src.utils.config import ConfigManager
from src.utils.hashing import SHA256_ID, SHA3_256_ID

env_manager = ConfigManager()

QUARANTINE_FILE = "quarantine.json"
# The hashlib names of the algorithms that a tag is given in, with their celaut id.
TAG_ALGORITHMS: Tuple[Tuple[str, bytes], ...] = (("sha256", SHA256_ID), ("sha3_256", SHA3_256_ID))


# ---------------------------------------------------------------------- config


def _section() -> dict:
    value = env_manager.get("devices") or {}
    return value if isinstance(value, dict) else {}


def enabled() -> bool:
    return _section().get("ENABLED") is True


def allowed() -> List[str]:
    """``devices.ALLOW`` as normalized PCI addresses, in order, without repeats."""
    out: List[str] = []
    for value in _section().get("ALLOW") or []:
        address = host.normalize_address(str(value))
        if address and address not in out:
            out.append(address)
    return out


def chip_settings(chip: str) -> dict:
    value = (_section().get("BY_CHIP") or {}).get(chip) or {}
    return value if isinstance(value, dict) else {}


def chip_price_mu_per_second(chip: str) -> Optional[int]:
    value = chip_settings(chip).get("mu_per_second")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


# --------------------------------------------------------------------- kernels


@dataclass(frozen=True)
class OfferedKernel:
    hash: str  # the kernel store hash (SHA-256 hex)
    path: str
    digests: Tuple[Tuple[str, str], ...]  # ((algorithm id hex, digest hex), ...)
    claims: Tuple[str, ...] = ()  # older SHA-256 hashes (KERNEL_ABI_ALIASES)


_DIGEST_CACHE: Dict[Tuple[str, int, int, int], Tuple[Tuple[str, str], ...]] = {}


def _digests(path: str) -> Tuple[Tuple[str, str], ...]:
    from src.virtualizers.microvm import kernel_store

    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns, st.st_ino)
    cached = _DIGEST_CACHE.get(key)
    if cached is None:
        hexes = kernel_store.file_hashes(path, tuple(name for name, _ in TAG_ALGORITHMS))
        cached = tuple((ident.hex(), hexes[name]) for name, ident in TAG_ALGORITHMS)
        _DIGEST_CACHE[key] = cached
    return cached


def configured_kernel_hashes() -> List[str]:
    """``devices.KERNELS``; when empty, the hash of the kernel of new native builds."""
    from src.utils.arch_guard import host_arch_tag
    from src.virtualizers.microvm import kernel_store

    hashes = [str(h).lower() for h in (_section().get("KERNELS") or [])]
    if hashes:
        return hashes
    arch = host_arch_tag()
    path = (env_manager.get("virtualizers.ch.KERNEL_PATHS") or {}).get(arch or "")
    if path and os.path.isfile(path):
        try:
            return [kernel_store.file_hash(path)]
        except OSError:
            return []
    return []


def offered_kernels() -> Tuple[List[OfferedKernel], List[str]]:
    """``(kernels on this node, problems)`` for the configured kernel hashes.

    Exact hashes only: a device entry names the kernel that the guest boots. A
    kernel that is not in the store is a problem (doctor), not an entry.
    """
    from src.virtualizers.microvm import kernel_store

    aliases = kernel_store.abi_aliases()
    kernels: List[OfferedKernel] = []
    problems: List[str] = []
    for kernel_hash in configured_kernel_hashes():
        found = kernel_store._find_exact(kernel_hash)
        if found is None:
            problems.append(f"The device kernel {kernel_hash} is not in the kernel store.")
            continue
        try:
            digests = _digests(str(found))
        except OSError as e:
            problems.append(f"Could not read the device kernel {found}: {e}")
            continue
        kernels.append(OfferedKernel(
            hash=kernel_hash, path=str(found), digests=digests,
            claims=tuple(aliases.get(kernel_hash, ())),
        ))
    return kernels, problems


def tags_for(chip: str, kernel: OfferedKernel) -> frozenset:
    _, vendor, device = chip.split(":")
    tags = {spec.make_tag(vendor, device, alg, digest) for alg, digest in kernel.digests}
    tags.update(spec.make_tag(vendor, device, SHA256_ID, old) for old in kernel.claims)
    return frozenset(tags)


# ------------------------------------------------------------------ quarantine


def _devices_dir() -> Path:
    return Path(str(env_manager.get("main.STORAGE") or "/nodo/storage")) / "devices"


def quarantined() -> Dict[str, dict]:
    """``{address: {"reason", "at"}}``."""
    try:
        with open(_devices_dir() / QUARANTINE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_quarantine(data: Dict[str, dict]) -> None:
    folder = _devices_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".quarantine.", dir=str(folder))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, folder / QUARANTINE_FILE)


def quarantine(address: str, reason: str) -> None:
    data = quarantined()
    data[address] = {"reason": reason, "at": int(time.time())}
    _write_quarantine(data)


def clear_quarantine(address: Optional[str] = None) -> List[str]:
    """Clear one address (or all). Returns the addresses cleared."""
    data = quarantined()
    if address is None:
        cleared = sorted(data)
        data = {}
    else:
        cleared = [address] if data.pop(address, None) is not None else []
    _write_quarantine(data)
    return cleared


# ------------------------------------------------------------------- inventory


@dataclass
class DeviceState:
    """One allowlisted address, as ``nodo devices`` and doctor show it."""
    address: str
    report: host.DeviceReport
    chip: str = ""
    price_mu_per_second: Optional[int] = None
    quarantine: Optional[dict] = None
    assigned_to: Optional[str] = None
    entries: List[spec.NodeDevice] = field(default_factory=list)

    @property
    def offered(self) -> bool:
        return bool(self.entries)

    @property
    def state(self) -> str:
        if not self.report.usable:
            return "unusable"
        if self.price_mu_per_second is None:
            return "no price"
        if self.quarantine:
            return "quarantined"
        if not self.entries:
            return "no kernel"
        if self.assigned_to:
            return "assigned"
        return "free"


def _daemon_ids() -> Tuple[Optional[int], Sequence[int]]:
    """The uid and groups that open /dev/vfio: this process's (the daemon)."""
    try:
        return os.geteuid(), os.getgroups()
    except OSError:
        return None, ()


def states(
        *,
        with_assignments: bool = True,
        uid: Optional[int] = None,
        gids: Optional[Sequence[int]] = None,
        sysfs: str = host.SYSFS,
        dev_root: str = host.DEV,
) -> List[DeviceState]:
    """Every allowlisted address with its checks, price, kernels and state.

    ``uid``/``gids``: who opens /dev/vfio (default: this process, the daemon).
    """
    addresses = allowed()
    if not addresses:
        return []
    if uid is None:
        uid, own_gids = _daemon_ids()
        gids = own_gids if gids is None else gids
    reports = host.check_allowlist(addresses, uid=uid, gids=gids or (), sysfs=sysfs, dev_root=dev_root)
    kernels, _ = offered_kernels()
    jail = quarantined()
    taken: Dict[str, str] = {}
    if with_assignments:
        from src.devices import assignments

        taken = assignments.assigned()
    out: List[DeviceState] = []
    for report in reports:
        chip = report.pci.chip
        st = DeviceState(
            address=report.address, report=report, chip=chip,
            price_mu_per_second=chip_price_mu_per_second(chip) if chip else None,
            quarantine=jail.get(report.address), assigned_to=taken.get(report.address),
        )
        if report.usable and st.price_mu_per_second is not None and not st.quarantine:
            settings = chip_settings(chip)
            mem = settings.get("mem_limit")
            bench = settings.get("benchmark") or {}
            for kernel in kernels:
                st.entries.append(spec.NodeDevice(
                    address=report.address, chip=chip, kernel_hash=kernel.hash,
                    tags=tags_for(chip, kernel),
                    mem_limit=int(mem) if isinstance(mem, int) and not isinstance(mem, bool) else None,
                    benchmark=tuple(sorted((str(k), int(v)) for k, v in bench.items())),
                    meta={"kernel_path": kernel.path},
                ))
        out.append(st)
    return out


def node_devices(**kwargs) -> List[spec.NodeDevice]:
    """Every entry that this node offers (assigned ones too). Empty when disabled."""
    if not enabled():
        return []
    return [e for st in states(**kwargs) for e in st.entries]


def free_devices(**kwargs) -> List[spec.NodeDevice]:
    """The entries whose physical device no instance holds now."""
    if not enabled():
        return []
    return [e for st in states(**kwargs) if not st.assigned_to for e in st.entries]


def announced() -> List[celaut.Device]:
    """``Peer.devices``: capacity, so assigned devices are announced too."""
    return [e.to_proto() for e in node_devices(with_assignments=False)]


def report(**kwargs) -> dict:
    """``nodo devices --json`` (the TUI reads it) and the data of the doctor section."""
    kernels, problems = offered_kernels()
    out_states = states(**kwargs)
    return {
        "enabled": enabled(),
        "kernels": [{"hash": k.hash, "path": k.path, "claims": list(k.claims)} for k in kernels],
        "problems": problems,
        "devices": [
            {
                "address": st.address,
                "chip": st.chip,
                "state": st.state,
                "price_mu_per_second": st.price_mu_per_second,
                "watts": chip_settings(st.chip).get("watts") if st.chip else None,
                "mem_limit": st.entries[0].mem_limit if st.entries else None,
                "quarantine": st.quarantine,
                "assigned_to": st.assigned_to,
                "kernels": [e.kernel_hash for e in st.entries],
                "tags": sorted(t for e in st.entries for t in e.tags),
                "findings": [
                    {"level": f.level, "message": f.message, "suggestion": f.suggestion}
                    for f in st.report.findings
                ],
            }
            for st in out_states
        ],
    }
