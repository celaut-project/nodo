"""The floors and defaults a CH guest is actually given, in one place.

Every number here is applied twice: when an instance is created, to decide what the
guest gets, and when it is priced, to decide what the client is quoted. Both sides
import this module, so a manifest resolves the same way whichever one asks -- a floor
defined anywhere else is a price the node charges without quoting it.

Deliberately dependency-light: protos and config only. No logger (importing it
creates the storage directory), no filesystem reads, nothing from the virtualizer
proper -- so the pricing layer can import this without importing a hypervisor, and
so it stays unit-testable on a bare checkout.
"""
import math
from typing import Optional, Tuple

from protos import celaut_pb2
from src.utils.config import ConfigManager

env_manager = ConfigManager()


def _env_int(key: str, default: int) -> int:
    try:
        return int(env_manager.get(key, default))
    except Exception:
        return int(default)


# Guest CPU/RAM. A service that declares no CPU gets one vCPU, and one that declares
# less RAM than MIN_MEM_MIB is rounded up to it -- below that the kernel plus
# initramfs never reaches console, so the floor is a boot requirement, not a policy.
DEFAULT_VCPUS = 1
DEFAULT_MEM_MIB = max(16, _env_int("virtualizers.ch.DEFAULT_MEM_MIB", 256))
MIN_MEM_MIB = max(16, _env_int("virtualizers.ch.MIN_MEM_MIB", 128))
if DEFAULT_MEM_MIB < MIN_MEM_MIB:
    DEFAULT_MEM_MIB = MIN_MEM_MIB

# Rootfs image. The build floors the image at MIN_ROOTFS_BYTES and at the populated
# tree plus OVERHEAD_BYTES for filesystem metadata, then grows it by
# MKFS_GROWTH_FACTOR on every mkfs.ext4 out-of-space retry. All three make the image
# larger than the manifest asked for; none of them can make it smaller.
OVERHEAD_BYTES = 64 * 1024 * 1024
MIN_ROOTFS_BYTES = 128 * 1024 * 1024
MKFS_GROWTH_FACTOR = 2

# Guest kernel overhead: the memory counterpart of OVERHEAD_BYTES above.
#
# A manifest's `mem_limit` is a promise to the *service*: memory the service may
# use. The hypervisor's `-m` is not that figure. The guest kernel takes its text,
# its rodata/rwdata, its percpu areas and -- growing with the size of the guest --
# one `struct page` per 4 KiB frame, all before init runs. What is left is what the
# service can actually allocate, and it is materially less.
#
# Booting a guest at exactly `mem_limit` therefore hands the service less RAM than
# its manifest declared. Measured on the 6.12 arm64 guest kernel this node ships,
# from the `Memory: <avail>K/<total>K available` boot line:
#
#     -m 128 MiB  ->  102.7 MiB usable  (25.3 MiB overhead)
#     -m 256 MiB  ->  228.1 MiB usable  (27.9 MiB overhead)
#
# i.e. a fixed cost of ~23 MiB plus ~2% of the guest's RAM (the `struct page`
# array, 64 bytes per 4 KiB page). A service declaring `at_most 256 MiB` is
# OOM-killed at ~228 MiB -- below its own declared ceiling.
#
# Disk already works this way: `initial_rootfs_size_bytes` grows the image by
# OVERHEAD_BYTES so a service asking for N bytes of disk can store N bytes rather
# than N minus the filesystem's metadata. Memory simply never got the same
# treatment. The defaults carry margin over the measured figures because the
# overhead varies with kernel version, architecture and config, and the failure
# mode of too little is a guest OOM. Both settings at zero restores the previous
# behaviour exactly.
GUEST_KERNEL_RESERVE_BYTES = max(0, _env_int("virtualizers.ch.GUEST_KERNEL_RESERVE_MIB", 32)) * 1024 * 1024
try:
    GUEST_KERNEL_RESERVE_RATIO = max(
        0.0, float(env_manager.get("virtualizers.ch.GUEST_KERNEL_RESERVE_RATIO", 0.05))
    )
except Exception:
    GUEST_KERNEL_RESERVE_RATIO = 0.05


def guest_boot_memory_bytes(usable_bytes: int) -> int:
    """Hypervisor allocation so a guest can really offer ``usable_bytes`` to userspace.

    Deliberately NOT part of :func:`resolve_initial_resources`, and so not part of
    what :func:`billable_resources` returns. Two separate figures are wanted and
    conflating them breaks something either way:

    * ``resolve_initial_resources`` answers "what does this instance hold", which is
      what the ``local_instances`` row records and the maintenance tick charges. It
      has to stay idempotent -- it is applied to the manifest at launch and re-applied
      to the already-resolved row when pricing it, and a figure that grew on each
      application would make a quote and its charge disagree.
    * this answers "how big must the VM be for that to be true", which only the two
      backends need, at the one point where they build the hypervisor argument.

    Keeping the overhead out of the billable figure also means the node absorbs it
    rather than the client: a client is charged for the RAM it asked for and can use,
    never for the kernel underneath it.
    """
    usable_bytes = int(usable_bytes)
    if usable_bytes <= 0:
        return usable_bytes
    reserve = GUEST_KERNEL_RESERVE_BYTES + int(
        math.ceil(usable_bytes * GUEST_KERNEL_RESERVE_RATIO)
    )
    return usable_bytes + reserve


def resolve_initial_resources(resources: celaut_pb2.Sysresources) -> Tuple[int, int, int, int]:
    """(vcpus, mem_bytes, cpu_quota, cpu_period) the instance holds for `resources`.

    ``mem_bytes`` is memory the *service* may use -- what the row records and what it
    is billed for. The VM is booted somewhat larger than this so that figure is actually
    deliverable; see :func:`guest_boot_memory_bytes`.
    """
    vcpus = DEFAULT_VCPUS
    mem_b = DEFAULT_MEM_MIB * (1024 * 1024)  # Convert MiB to bytes

    # Default values: 1 vCPU
    cpu_period = 100000
    cpu_quota = 100000

    try:
        if resources:
            if resources.HasField("cpu_period") and resources.cpu_period > 0:
                cpu_period = resources.cpu_period

            if resources.HasField("cpu_quota") and resources.cpu_quota > 0:
                cpu_quota = resources.cpu_quota

            if cpu_period > 0 and cpu_quota > 0:
                vcpus = max(1, int(math.ceil(cpu_quota / cpu_period)))

            if resources.HasField("mem_limit") and resources.mem_limit > 0:
                # mem_b is in bytes; MIN_MEM_MIB is a MiB floor, so convert it to
                # bytes before comparing. Without the conversion the floor is a
                # no-op (128 < any real byte count) and a service declaring e.g.
                # mem_limit=50MB boots with ~48 MiB of guest RAM — too little for
                # the kernel+initramfs to reach console, so the guest never brings
                # up eth0 and launch fails with "Guest network did not become ready".
                mem_b = max(MIN_MEM_MIB * 1024 * 1024, int(resources.mem_limit))
    except Exception:
        pass

    return vcpus, mem_b, cpu_quota, cpu_period


def resolve_boot_mem_bytes(resources: Optional[celaut_pb2.Service.Container.Resources]) -> int:
    """RAM a guest must be *booted* with to be able to reach its declared ``at_most``.

    Not the same question as :func:`resolve_initial_resources`, which answers "what
    does the guest start out holding". A backend whose only memory knob is the cgroup
    (Cloud Hypervisor) never needs this: it can raise ``memory.max`` at any point, so
    booting at ``at_init`` costs it nothing. QEMU can not -- ``-m`` is fixed for the
    life of the process and the balloon can only deflate back up to it -- so a guest
    booted at ``at_init`` can never be grown, however much headroom its manifest
    declared. Reserving ``at_most`` up front is what makes that headroom reachable;
    the difference is then held by the balloon rather than by the guest, so what the
    guest *has* is still ``at_init`` (see
    :func:`src.virtualizers.qemu.hotplug.settle_boot_balloon`).

    Never below the ``at_init`` figure, floors included: a manifest whose ``at_most``
    is unset, zero, or smaller than its ``at_init`` declares no headroom, and a guest
    with no headroom to reserve boots at exactly the figure it was granted.

    Reserving the ceiling is not an over-commitment the node has not already vetted:
    admission control quotes and rejects on ``at_most`` (see
    ``get_resource_availability``), so a manifest that reaches a launch is one whose
    ceiling the node already said it could afford.
    """
    _, init_mem_b, _, _ = resolve_initial_resources(resources.at_init if resources else None)

    most_mem_b = 0
    try:
        if resources is not None and resources.HasField("at_most") and resources.at_most.HasField("mem_limit"):
            most_mem_b = int(resources.at_most.mem_limit)
    except Exception:
        most_mem_b = 0

    return max(init_mem_b, most_mem_b)


def requested_disk_space_bytes(service: celaut_pb2.Service) -> Optional[int]:
    """The disk figure a service's manifest asks for, or None if it names none."""
    try:
        resources = service.container.resources
    except Exception:
        return None

    requested_bytes = 0
    for scope_name in ("at_init", "at_most"):
        scope = getattr(resources, scope_name, None)
        if scope is None:
            continue
        try:
            value = int(getattr(scope, "disk_space", 0) or 0)
            return value if value > 0 else None  # if disk_space is set in at_init, we use it directly as the requested size
        except Exception:
            value = 0
        if value > requested_bytes:
            requested_bytes = value

    return requested_bytes if requested_bytes > 0 else None


def initial_rootfs_size_bytes(service: celaut_pb2.Service, total_bytes: int) -> int:
    """Size to format the rootfs image at, given the tree it has to hold."""
    return max(
        MIN_ROOTFS_BYTES,
        int(total_bytes) + OVERHEAD_BYTES,
        int(requested_disk_space_bytes(service) or 0),
    )


def billable_resources(
    declared: Optional[celaut_pb2.Sysresources],
    built_rootfs_size_bytes: Optional[int] = None,
) -> celaut_pb2.Sysresources:
    """What an instance holding `declared` will actually be billed for.

    The same resolution the virtualizer performs when it creates the guest, applied
    before there is a guest, so a price quoted for a manifest matches the charge the
    maintenance tick levies against the row (`local_instances`): undeclared CPU is one
    vCPU, RAM is at least MIN_MEM_MIB, and the rootfs image is at least
    MIN_ROOTFS_BYTES.

    ``built_rootfs_size_bytes`` is the size of the image an already-built service
    hands its instances -- the exact figure the tick prices, when the caller can look
    it up. Without it, disk is a lower bound: the populated-tree floor and the mkfs
    growth retries are known only once the image exists, so a service not yet built
    here can cost more than this says. It can never cost less, which is the direction
    that matters -- a client is never billed above its quote.
    """
    _, mem_b, cpu_quota, cpu_period = resolve_initial_resources(declared)

    declared_disk = 0
    try:
        if declared is not None and declared.HasField("disk_space"):
            declared_disk = int(declared.disk_space)
    except Exception:
        declared_disk = 0

    return celaut_pb2.Sysresources(
        mem_limit=mem_b,
        cpu_period=cpu_period,
        cpu_quota=cpu_quota,
        disk_space=max(MIN_ROOTFS_BYTES, declared_disk, int(built_rootfs_size_bytes or 0)),
    )
