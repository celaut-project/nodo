"""The ``Device`` messages of celaut.proto: tags, validation, matching, assignment (#536).

Tag format (fixed by celaut.proto, ``message Device``)::

    pci:<vendor>:<device>+<algorithm>:<hash>

* ``<vendor>``, ``<device>``: the PCI identifiers of the chip, 4 lowercase hex digits
  (as sysfs gives them, without ``0x``).
* ``<algorithm>``: the celaut identifier of a hash algorithm, its output for the empty
  input, in lowercase hex (for example the ``SHA3_256_ID`` of ``src/utils/hashing.py``).
  No names: nobody has to agree on what "sha256" means.
* ``<hash>``: that algorithm's output for the guest kernel file (vmlinuz), lowercase hex.

The tags of one Device are synonyms of ONE architecture (one chip and one kernel),
never alternatives. A node entry and a service entry match when they share a tag.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from protos import celaut_pb2 as celaut
from src.utils import keyvalue

TAG_PREFIX = "pci"
_HEX = r"[0-9a-f]"
_TAG_RE = re.compile(
    rf"^pci:(?P<vendor>{_HEX}{{4}}):(?P<device>{_HEX}{{4}})"
    rf"\+(?P<algorithm>{_HEX}{{16,256}}):(?P<hash>{_HEX}{{16,256}})$"
)
# The fields of Sysresources that must be equal in at_init and at_most when a
# service has devices (decision 8 of #536): VFIO pins all the guest memory, so the
# node cannot change the resources of an instance that has devices.
FIXED_RESOURCE_FIELDS = ("blkio_weight", "cpu_period", "cpu_quota", "mem_limit", "disk_space")

ATTACH_AT_START = celaut.Device.ATTACH_AT_START
ATTACH_ON_REQUEST = celaut.Device.ATTACH_ON_REQUEST
# The attach modes that this node supports. Version 1: only at the start.
SUPPORTED_ATTACH = (ATTACH_AT_START,)


@dataclass(frozen=True)
class DeviceTag:
    vendor: str
    device: str
    algorithm: str
    hash: str

    @property
    def chip(self) -> str:
        return chip_key(self.vendor, self.device)

    @property
    def kernel(self) -> Tuple[str, str]:
        return self.algorithm, self.hash

    def __str__(self) -> str:
        return make_tag(self.vendor, self.device, self.algorithm, self.hash)


def chip_key(vendor: str, device: str) -> str:
    """The chip part of a tag, ``pci:<vendor>:<device>``: the key of devices.BY_CHIP."""
    return f"{TAG_PREFIX}:{vendor.lower()}:{device.lower()}"


def make_tag(vendor: str, device: str, algorithm, digest: str) -> str:
    if isinstance(algorithm, (bytes, bytearray)):
        algorithm = bytes(algorithm).hex()
    return f"{chip_key(vendor, device)}+{algorithm.lower()}:{digest.lower()}"


def parse_tag(tag: str) -> Optional[DeviceTag]:
    """The parts of a device tag, or None when it is not in the format."""
    m = _TAG_RE.match(tag or "")
    if not m:
        return None
    return DeviceTag(m["vendor"], m["device"], m["algorithm"], m["hash"])


def parsed_tags(device: celaut.Device) -> List[DeviceTag]:
    return [t for t in (parse_tag(x) for x in device.architecture.tags) if t]


# ---------------------------------------------------------------------- attach


def chosen_attach(device: celaut.Device) -> Optional[int]:
    """The attach mode that this node uses for a service entry, or None (refuse).

    celaut.proto: the node uses the first value in the list that it supports; an
    empty list is ATTACH_AT_START only; a value that it does not know is skipped.
    """
    if not len(device.attach):
        return ATTACH_AT_START
    for mode in device.attach:
        if mode in SUPPORTED_ATTACH:
            return mode
    return None


# ------------------------------------------------------------------ validation


def _set_fields(sys: celaut.Sysresources) -> Dict[str, int]:
    return {f: getattr(sys, f) for f in FIXED_RESOURCE_FIELDS if sys.HasField(f)}


def device_reason(device: celaut.Device, index: int = 0) -> Optional[str]:
    """Why one Device entry of a service (or of a request) is not valid, or None."""
    where = f"Device {index}"
    tags = list(device.architecture.tags)
    if not tags:
        return f"{where} has no architecture tags. An empty device architecture is not valid."
    parsed = parsed_tags(device)
    if not parsed:
        return (
            f"{where}: no tag is in the format pci:<vendor>:<device>+<algorithm>:<hash> "
            f"(lowercase hexadecimal)."
        )
    chips = {t.chip for t in parsed}
    if len(chips) > 1:
        return (
            f"{where} names more than one chip ({', '.join(sorted(chips))}). The tags of a "
            f"Device are synonyms of one architecture, never alternatives: publish one "
            f"variant of the service for each chip."
        )
    per_algorithm: Dict[str, str] = {}
    for t in parsed:
        if per_algorithm.setdefault(t.algorithm, t.hash) != t.hash:
            return (
                f"{where} names two kernels with the same hash algorithm. The tags of a "
                f"Device are synonyms of one architecture (one chip and one kernel)."
            )
    if chosen_attach(device) is None:
        return (
            f"{where} accepts only attach modes that this node does not support "
            f"(this node attaches devices only at the start: ATTACH_AT_START)."
        )
    res = device.resources
    if res.HasField("at_init") and res.HasField("at_most"):
        if (res.at_init.HasField("mem_limit") and res.at_most.HasField("mem_limit")
                and res.at_init.mem_limit > res.at_most.mem_limit):
            return f"{where}: the at_init memory is more than the at_most memory."
    return None


def kernels_of(devices: Sequence[celaut.Device]) -> Dict[str, str]:
    """``{algorithm: hash}`` over all the devices; raises ValueError on a conflict."""
    out: Dict[str, str] = {}
    for d in devices:
        for t in parsed_tags(d):
            if out.setdefault(t.algorithm, t.hash) != t.hash:
                raise ValueError(t.algorithm)
    return out


def devices_reason(devices: Sequence[celaut.Device]) -> Optional[str]:
    """Why a list of devices (a service's, or a request's) is not valid, or None."""
    for i, d in enumerate(devices):
        reason = device_reason(d, i)
        if reason:
            return reason
    try:
        kernels_of(devices)
    except ValueError:
        return (
            "The devices of the service name different guest kernels. All the devices "
            "of one service share one kernel: one virtual machine boots one kernel."
        )
    return None


def container_reason(container: celaut.Service.Container) -> Optional[str]:
    """Why the devices of a container are not valid, or None (also with no devices).

    With devices, the resources must have the same at_init and at_most (decision 8 of
    #536): the node cannot change the resources of an instance that has devices.
    """
    if not len(container.devices):
        return None
    reason = devices_reason(container.devices)
    if reason:
        return reason
    res = container.resources if container.HasField("resources") else None
    if res is None or not res.HasField("at_init") or not res.HasField("at_most"):
        return (
            "A service with devices must declare its resources with at_init equal to "
            "at_most: the node cannot change the resources of an instance that has devices."
        )
    if _set_fields(res.at_init) != _set_fields(res.at_most):
        return (
            "A service with devices must have the same at_init and at_most resources: "
            "the node cannot change the resources of an instance that has devices."
        )
    return None


def service_reason(service: celaut.Service) -> Optional[str]:
    if not service.HasField("container"):
        return None
    return container_reason(service.container)


# -------------------------------------------------------------------- matching


@dataclass(frozen=True)
class NodeDevice:
    """One device that the node can give, with one kernel: one Peer.devices entry.

    ``address`` is the physical device (its PCI address); one card with two kernels
    is two entries with the same address.
    """
    address: str
    chip: str
    kernel_hash: str  # the kernel store hash (SHA-256 hex)
    tags: FrozenSet[str]
    mem_limit: Optional[int] = None
    benchmark: Tuple[Tuple[str, int], ...] = ()
    attach: Tuple[int, ...] = (ATTACH_AT_START,)
    meta: Dict[str, object] = field(default_factory=dict, compare=False, hash=False)

    def to_proto(self) -> celaut.Device:
        d = celaut.Device()
        d.architecture.tags.extend(sorted(self.tags))
        at_most = d.resources.at_most
        if self.mem_limit is not None:
            at_most.mem_limit = int(self.mem_limit)
        for k, v in self.benchmark:
            at_most.benchmark.append(celaut.Uint64KeyValue(key=k, value=int(v)))
        d.attach.extend(self.attach)
        return d


def entry_misfit(asked: celaut.Device, entry: NodeDevice) -> Optional[str]:
    """Why a node entry cannot serve a requested device, or None."""
    if not (set(asked.architecture.tags) & entry.tags):
        return "no tag in common"
    mode = chosen_attach(asked)
    if mode is None or mode not in entry.attach:
        return "attach mode"
    init = asked.resources.at_init if asked.resources.HasField("at_init") else None
    if init is not None:
        if init.HasField("mem_limit") and entry.mem_limit is not None \
                and init.mem_limit > entry.mem_limit:
            return f"memory {init.mem_limit} is above {entry.mem_limit}"
        scores = dict(entry.benchmark)
        for k, v in keyvalue.to_dict(init.benchmark).items():
            if k in scores and v > scores[k]:
                return f"benchmark {k} {v} is above {scores[k]}"
    return None


def _matching(asked: Sequence[celaut.Device], entries: Sequence[NodeDevice]) -> Optional[List[NodeDevice]]:
    """A maximum bipartite matching (augmenting paths) with distinct addresses."""
    fits = [[j for j, e in enumerate(entries) if entry_misfit(a, e) is None] for a in asked]
    owner_of_address: Dict[str, int] = {}
    choice: Dict[int, int] = {}

    def augment(i: int, seen: set) -> bool:
        for j in fits[i]:
            addr = entries[j].address
            if addr in seen:
                continue
            seen.add(addr)
            holder = owner_of_address.get(addr)
            if holder is None or augment(holder, seen):
                owner_of_address[addr] = i
                choice[i] = j
                return True
        return False

    # The most constrained first: fewer candidates, fewer retries.
    for i in sorted(range(len(asked)), key=lambda x: len(fits[x])):
        if not augment(i, set()):
            return None
    return [entries[choice[i]] for i in range(len(asked))]


def assign(
        asked: Sequence[celaut.Device],
        free: Sequence[NodeDevice],
) -> Tuple[Optional[List[NodeDevice]], str]:
    """An exclusive assignment of free node entries to the asked devices.

    ``(entries, "")`` with one entry per asked device, in order: distinct physical
    devices, all with ONE kernel. ``(None, reason)`` when there is none. The node
    tries the combinations (decision of 11/11: "when the minimums are different").
    """
    if not asked:
        return [], ""
    by_kernel: Dict[str, List[NodeDevice]] = {}
    for e in free:
        by_kernel.setdefault(e.kernel_hash, []).append(e)
    for kernel_hash in sorted(by_kernel):
        found = _matching(asked, by_kernel[kernel_hash])
        if found is not None:
            return found, ""
    if not free:
        return None, "This node has no free device."
    for i, a in enumerate(asked):
        if not any(entry_misfit(a, e) is None for e in free):
            return None, f"This node has no free device for Device {i} ({', '.join(a.architecture.tags)})."
    return None, (
        f"This node cannot give {len(asked)} free devices of these architectures "
        f"at the same time, with one kernel."
    )


def announced_misfit(asked: Sequence[celaut.Device], announced: Iterable[celaut.Device]) -> Optional[str]:
    """Why a peer that announced ``announced`` (Peer.devices) could never give ``asked``.

    By tag and count only (the announcement is a capacity, not the free devices now;
    GetResourceAvailability gives the real answer). An entry repeated is a device
    repeated, so each announced entry serves at most one asked device. None: ask it.
    """
    if not asked:
        return None
    entries = []
    for n, d in enumerate(announced):
        mem = d.resources.at_most.mem_limit if d.resources.at_most.HasField("mem_limit") else None
        tags = frozenset(d.architecture.tags)
        kernel = ",".join(sorted(f"{t.algorithm}:{t.hash}" for t in (parse_tag(x) for x in tags) if t))
        entries.append(NodeDevice(
            address=f"#{n}", chip="", kernel_hash=kernel, tags=tags, mem_limit=mem,
            benchmark=tuple(keyvalue.to_dict(d.resources.at_most.benchmark).items()),
            attach=tuple(d.attach) or (ATTACH_AT_START,),
        ))
    if not entries:
        return "it announced no devices"
    # A peer's kernels are known by their tags only; group by the tag set.
    found, reason = assign(asked, entries)
    return None if found is not None else reason.replace("This node", "it")
