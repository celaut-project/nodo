"""The operator's devices as the host sees them: sysfs, IOMMU groups, VFIO (#536).

The operator binds a device to ``vfio-pci`` and lists its PCI address in
``devices.ALLOW``. nodo never unbinds a host driver. The chip is read from sysfs,
never from the config: a node does not trust a name that someone typed.

Every function takes the sysfs and /dev roots as arguments, so tests run on a fake
tree. The checks return :class:`Finding` values; ``nodo doctor`` prints them and
:mod:`inventory` offers only the devices with no FAIL.
"""
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from src.devices.spec import chip_key

SYSFS = "/sys"
DEV = "/dev"
VFIO_DRIVER = "vfio-pci"
# PCI class 0x0604xx: a PCI-to-PCI bridge. A bridge in a group is not a device that
# a guest could use, and vfio allows it to keep its driver (pcieport).
BRIDGE_CLASS_PREFIX = "0x0604"
_ADDRESS_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class Finding:
    level: str
    message: str
    suggestion: str = ""


@dataclass
class PciDevice:
    address: str
    vendor: str = ""
    device: str = ""
    pci_class: str = ""
    driver: str = ""
    iommu_group: str = ""
    reset_methods: Tuple[str, ...] = ()
    can_reset: bool = False
    exists: bool = False

    @property
    def chip(self) -> str:
        return chip_key(self.vendor, self.device) if self.vendor and self.device else ""

    @property
    def is_bridge(self) -> bool:
        return self.pci_class.startswith(BRIDGE_CLASS_PREFIX)


def normalize_address(address: str) -> Optional[str]:
    """``0b:00.0`` or ``0000:0B:00.0`` -> ``0000:0b:00.0``; None when not an address."""
    value = str(address or "").strip().lower()
    if re.match(r"^[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$", value):
        value = "0000:" + value
    return value if _ADDRESS_RE.match(value) else None


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _link_name(path: str) -> str:
    try:
        return os.path.basename(os.readlink(path))
    except OSError:
        return ""


def device_dir(address: str, sysfs: str = SYSFS) -> str:
    return os.path.join(sysfs, "bus", "pci", "devices", address)


def read_device(address: str, sysfs: str = SYSFS) -> PciDevice:
    base = device_dir(address, sysfs)
    dev = PciDevice(address=address)
    if not os.path.isdir(base):
        return dev
    dev.exists = True
    dev.vendor = _read(os.path.join(base, "vendor")).lower().removeprefix("0x")
    dev.device = _read(os.path.join(base, "device")).lower().removeprefix("0x")
    dev.pci_class = _read(os.path.join(base, "class")).lower()
    dev.driver = _link_name(os.path.join(base, "driver"))
    dev.iommu_group = _link_name(os.path.join(base, "iommu_group"))
    methods = _read(os.path.join(base, "reset_method"))
    dev.reset_methods = tuple(methods.split()) if methods else ()
    dev.can_reset = os.path.exists(os.path.join(base, "reset"))
    return dev


def group_members(group: str, sysfs: str = SYSFS) -> List[str]:
    path = os.path.join(sysfs, "kernel", "iommu_groups", group, "devices")
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def iommu_enabled(sysfs: str = SYSFS) -> bool:
    try:
        return bool(os.listdir(os.path.join(sysfs, "kernel", "iommu_groups")))
    except OSError:
        return False


def vfio_pci_loaded(sysfs: str = SYSFS) -> bool:
    return os.path.isdir(os.path.join(sysfs, "bus", "pci", "drivers", VFIO_DRIVER))


def responds(address: str, sysfs: str = SYSFS) -> bool:
    """The device answers config reads: its vendor id is not 0xffff (fell off the bus)."""
    try:
        with open(os.path.join(device_dir(address, sysfs), "config"), "rb") as f:
            head = f.read(2)
    except OSError:
        return False
    return len(head) == 2 and head != b"\xff\xff"


def reset_finding(dev: PciDevice) -> Finding:
    if "flr" in dev.reset_methods:
        return Finding(OK, f"{dev.address} has a function level reset (FLR).")
    if dev.can_reset:
        methods = ", ".join(dev.reset_methods) or "unknown"
        return Finding(
            WARN,
            f"{dev.address} has no FLR; the kernel resets it with: {methods}. The node "
            f"offers it, but a reset other than FLR may not clear all of its state.",
        )
    return Finding(
        FAIL,
        f"{dev.address} cannot be reset. The node does not offer a device that it "
        f"cannot reset between two tenants.",
        "Use another device, or a host kernel and firmware that give it a reset method.",
    )


def group_findings(dev: PciDevice, allowed: Sequence[str], sysfs: str = SYSFS) -> List[Finding]:
    """The whole IOMMU group must be safe to give to one guest (vfio 'viable')."""
    out: List[Finding] = []
    if not dev.iommu_group:
        return [Finding(
            FAIL, f"{dev.address} is in no IOMMU group: the IOMMU is off.",
            "Turn on the IOMMU (BIOS: VT-d/AMD-Vi; kernel: intel_iommu=on or amd_iommu=on).",
        )]
    members = group_members(dev.iommu_group, sysfs)
    others_allowed = []
    for member in members:
        if member == dev.address:
            continue
        m = read_device(member, sysfs)
        if m.is_bridge:
            continue
        if m.driver and m.driver != VFIO_DRIVER:
            out.append(Finding(
                FAIL,
                f"IOMMU group {dev.iommu_group} of {dev.address} is not complete: "
                f"{member} uses the host driver '{m.driver}'.",
                f"Bind every device of group {dev.iommu_group} to vfio-pci (nodo never "
                f"unbinds a host driver), or move the card to a slot with its own group.",
            ))
        if member in allowed:
            others_allowed.append(member)
    if others_allowed:
        out.append(Finding(
            FAIL,
            f"{dev.address} shares IOMMU group {dev.iommu_group} with "
            f"{', '.join(others_allowed)}, also in devices.ALLOW. One group goes to one "
            f"instance, so the node offers only the first address of a group.",
            "List only one address of each IOMMU group in devices.ALLOW.",
        ))
    if not out:
        out.append(Finding(OK, f"IOMMU group {dev.iommu_group} of {dev.address} is complete "
                               f"({len(members)} device(s))."))
    return out


def vfio_dev_findings(dev: PciDevice, uid: Optional[int], gids: Sequence[int], dev_root: str = DEV) -> List[Finding]:
    """/dev/vfio/vfio and /dev/vfio/<group> must be open for read and write by the daemon."""
    out: List[Finding] = []
    for name in ("vfio", dev.iommu_group):
        if not name:
            continue
        path = os.path.join(dev_root, "vfio", name)
        try:
            st = os.stat(path)
        except OSError:
            out.append(Finding(FAIL, f"{path} does not exist.",
                               "Load vfio-pci and bind the device to it."))
            continue
        if uid is None or uid == 0:
            continue
        mode = st.st_mode
        if st.st_uid == uid:
            ok = mode & 0o600 == 0o600
        elif st.st_gid in gids:
            ok = mode & 0o060 == 0o060
        else:
            ok = mode & 0o006 == 0o006
        if not ok:
            out.append(Finding(
                FAIL, f"{path} is not readable and writable by the service user (uid {uid}).",
                f"chgrp <service group> {path} && chmod g+rw {path} (a udev rule makes it persist).",
            ))
    if not out:
        out.append(Finding(OK, f"/dev/vfio/{dev.iommu_group} is usable by the daemon."))
    return out


def parse_memlock(limits_text: str) -> Optional[int]:
    """The soft 'Max locked memory' of /proc/<pid>/limits in bytes; -1 = unlimited."""
    for line in limits_text.splitlines():
        if line.startswith("Max locked memory"):
            parts = line.split()
            soft = parts[3] if len(parts) > 3 else ""
            if soft == "unlimited":
                return -1
            try:
                return int(soft)
            except ValueError:
                return None
    return None


def memlock_finding(uid: Optional[int], limits_text: Optional[str], largest_guest_bytes: int) -> Finding:
    """VFIO pins all the guest memory: RLIMIT_MEMLOCK must cover the largest guest."""
    if uid == 0:
        return Finding(OK, "The daemon runs as root (CAP_IPC_LOCK): RLIMIT_MEMLOCK does not apply.")
    if limits_text is None:
        return Finding(WARN, "The daemon is not running: RLIMIT_MEMLOCK was not checked.",
                       "Run nodo doctor again with the daemon running.")
    limit = parse_memlock(limits_text)
    if limit == -1:
        return Finding(OK, "RLIMIT_MEMLOCK of the daemon is unlimited.")
    if limit is None or limit < largest_guest_bytes:
        return Finding(
            FAIL,
            f"RLIMIT_MEMLOCK of the daemon ({limit} bytes) is below the memory of the "
            f"largest guest ({largest_guest_bytes} bytes). VFIO pins all the guest memory.",
            "Set LimitMEMLOCK=infinity in the nodo systemd unit (--service-user mode).",
        )
    return Finding(OK, f"RLIMIT_MEMLOCK of the daemon is {limit} bytes.")


@dataclass
class DeviceReport:
    address: str
    pci: PciDevice
    findings: List[Finding] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.pci.exists and not any(f.level == FAIL for f in self.findings)


def check_device(
        address: str,
        allowed: Sequence[str],
        *,
        uid: Optional[int] = None,
        gids: Sequence[int] = (),
        sysfs: str = SYSFS,
        dev_root: str = DEV,
) -> DeviceReport:
    """Every check for one allowlisted address (the memlock check is node-wide)."""
    dev = read_device(address, sysfs)
    report = DeviceReport(address=address, pci=dev)
    if not dev.exists:
        report.findings.append(Finding(FAIL, f"{address} is not a PCI device on this host."))
        return report
    report.findings.append(Finding(OK, f"{address} is {dev.chip} (class {dev.pci_class or '?'})."))
    if dev.driver != VFIO_DRIVER:
        report.findings.append(Finding(
            FAIL,
            f"{address} uses the driver '{dev.driver or 'none'}', not vfio-pci.",
            f"Bind it: echo {address} > /sys/bus/pci/devices/{address}/driver/unbind; "
            f"echo vfio-pci > /sys/bus/pci/devices/{address}/driver_override; "
            f"echo {address} > /sys/bus/pci/drivers_probe. nodo never unbinds a host driver.",
        ))
    report.findings.extend(group_findings(dev, allowed, sysfs))
    report.findings.append(reset_finding(dev))
    if dev.driver == VFIO_DRIVER:
        report.findings.extend(vfio_dev_findings(dev, uid, gids, dev_root))
    if not responds(address, sysfs):
        report.findings.append(Finding(FAIL, f"{address} does not answer config reads."))
    return report


def check_allowlist(allowed: Sequence[str], **kwargs) -> List[DeviceReport]:
    """Reports in allowlist order. The second address of a shared group is not usable."""
    reports = []
    for address in allowed:
        reports.append(check_device(address, allowed[: allowed.index(address)], **kwargs))
    return reports
