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
from typing import Dict, Optional, Tuple

from protos import celaut_pb2
from src.utils.arch_guard import (
    CANONICAL_ARCHITECTURES,
    host_arch_tag,
    normalize_arch_tag,
)
from src.utils.config import ConfigManager
from src.utils.filesystem_xattrs import READ_MODE_RO, read_mode

env_manager = ConfigManager()


def _env_int(key: str, default: int) -> int:
    try:
        return int(env_manager.get(key, default))
    except Exception:
        return int(default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(env_manager.get(key, default))
    except Exception:
        return float(default)


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
#
# All three exist to leave room for writes, so all three are skipped when the
# service declared `read_mode=ro` (see `is_read_only_service`): an immutable image
# has no writes to leave room for, and the floors are the whole of the 64 MiB flat
# tax issue #369 measured -- 44% of a 145 MiB capsule, 10x on a 12 MB one. A ro
# image is sized at its own bytes, and priced at them.
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
# its manifest declared.
#
# The overhead is ARCHITECTURE-DEPENDENT, so the reserve is too. Two guest kernels
# of the same version built for different architectures do not cost the same: the
# kernel image differs, and so do percpu layout, the early fixmap/identity mappings
# and how much firmware-reserved memory the platform hands back. Measured by booting
# the guest kernels this node ships and reading their own
# `Memory: <avail>K/<total>K available` line:
#
#   linux/arm64 (`virt`, QEMU/TCG)          linux/amd64 (Cloud Hypervisor/KVM)
#     -m  128 MiB -> 102.7 MiB  (25.3)        -m  128 MiB ->  95.1 MiB  (32.9)
#     -m  256 MiB -> 228.1 MiB  (27.9)        -m  256 MiB -> 220.7 MiB  (35.3)
#     -m  512 MiB -> 478.8 MiB  (33.2)        -m  512 MiB -> 472.0 MiB  (40.0)
#                                             -m 1024 MiB -> 974.8 MiB  (49.2)
#                                             -m 2048 MiB -> 1981.1 MiB (66.9)
#
#   fitted:  ~22.8 MiB + ~2.2% of the guest   fitted: ~31.4 MiB + ~1.8% of the guest
#
# Two parts, and only one of them is architectural:
#
#   * The FIXED part is what the arch costs. amd64's kernel image, its percpu areas
#     and its reserved low memory come to ~9 MiB more than arm64's. This is the
#     constant that has to depend on the architecture -- one shared value is either
#     wasteful on arm64 or too small on amd64, and too small means a guest OOM-killed
#     below the ceiling its manifest published, which is the shortfall this reserve
#     exists to close.
#
#   * The RATIO part is shared physics, not architecture: one `struct page` per 4 KiB
#     frame is 64 bytes, i.e. 1.56% of any guest, on both arches. Both measure near
#     2%. It is still settable per arch, because an operator running a kernel with a
#     different page size or `struct page` layout has no other way to correct it.
#
# Disk already works this way: `initial_rootfs_size_bytes` grows the image by
# OVERHEAD_BYTES so a service asking for N bytes of disk can store N bytes rather
# than N minus the filesystem's metadata. Memory is sized the same way here. A
# reserve of zero sizes the VM at exactly the usable figure.
#
# WHERE THE MARGIN GOES. Both parts carry margin over the measurements, because the
# overhead also varies with kernel version and config and the failure mode of too
# little is a guest OOM. But the two parts must not carry it the same way:
#
#   * The FIXED part is a constant, so margin on it is a constant. ~8 MiB over the
#     measured figure on either arch, and it stays ~8 MiB whatever the guest's size.
#     This is where generosity is cheap, and it is where kernel-version variance
#     actually lives: a different .config changes the image, the percpu areas and the
#     reserved regions, none of which scale with the guest.
#
#   * The RATIO part is multiplied by the guest, so margin on it is multiplied too. A
#     flat 5% against a measured ~1.8-2.1% is not caution, it is a growing tax the
#     node pays itself: on an 8 GiB guest it reserves 450 MiB where the kernel takes
#     ~180, and the node absorbs the difference as host RAM it committed and cannot
#     bill. The reserve is deliberately not billed (see `guest_boot_memory_bytes`),
#     so every over-reserved byte comes straight off what the operator earns per GiB
#     of RAM they own -- which is exactly the figure the TUI's pricing page exists to
#     show them.
#
# So the ratio is set near the physics instead. One `struct page` per 4 KiB frame at
# 64 bytes is 1.5625% of any guest and is the floor; the fitted measurements land at
# 1.80% (amd64) and 2.10% (arm64), the rest being early page tables and vmemmap
# alignment. 2.5% clears both with ~20-35% headroom -- room for a kernel built with a
# fatter `struct page` -- without scaling into hundreds of wasted MiB. An operator who
# has measured their own kernel can still tighten or widen it per arch.

# (fixed MiB, ratio) per canonical arch tag. Fitted against `usable` (what this
# function is given), not against `-m`: overhead = f + r*(usable + overhead), so the
# per-`-m` fits quoted above become 31.4 MiB + 1.80% and 23.1 MiB + 2.10% here.
# Overridable per arch under `virtualizers.ch.GUEST_KERNEL_RESERVE.<arch>`.
#
# The ratio is the same on both arches because the physics is: `struct page` per frame
# does not know what instruction set it is describing. It stays a per-arch *setting*
# only so an operator running one corrected kernel can fix it without touching the
# other. The fixed part is what genuinely differs -- amd64's kernel image, percpu
# areas and reserved low memory come to ~8 MiB more than arm64's.
_GUEST_KERNEL_RESERVE_RATIO = 0.025

_DEFAULT_GUEST_KERNEL_RESERVE = {
    # measured 23.1 MiB + 2.10%
    "linux/arm64": (32, _GUEST_KERNEL_RESERVE_RATIO),
    # measured 31.4 MiB + 1.80%
    "linux/amd64": (40, _GUEST_KERNEL_RESERVE_RATIO),
}

# Every arch nodo can name should have a measured reserve here, and
# `tests/test_tui_mirrors_the_node.py` fails if one does not: adding a tag to
# `arch_guard.ARCH_ALIASES` without measuring its guest kernel is a thing to notice.
#
# Deliberately a test rather than an import-time assert. The fallback below is safe --
# it over-reserves, it cannot under-reserve -- so an unmeasured arch is "please
# measure this", not "refuse to start". An assert here would take the node down at
# import for a table it could have degraded past, and it takes every *test* that
# imports this module down with it, as a skip labelled "missing runtime dependencies"
# rather than a failure. A guard that hides the other guards is worse than none.

# What an unknown arch gets: the largest reserve nodo has measured. An arch nobody
# has characterised here is more likely to resemble the most expensive kernel than
# the cheapest, and the cost of over-reserving is host RAM the node absorbs, while
# the cost of under-reserving is a service OOM-killed below its declared ceiling.
_FALLBACK_GUEST_KERNEL_RESERVE = max(
    _DEFAULT_GUEST_KERNEL_RESERVE.values(), key=lambda pair: (pair[0], pair[1])
)


def _reserve_for_arch(arch: Optional[str]) -> Tuple[int, float]:
    """(fixed bytes, ratio) reserved for a guest of ``arch``.

    Config overrides live under ``virtualizers.ch.GUEST_KERNEL_RESERVE.<arch>`` with
    ``MIB`` and ``RATIO`` keys, so an operator who has measured their own guest kernel
    can correct one architecture without touching the other. Both at zero size the VM
    at exactly the usable figure, for that arch alone.
    """
    # No arch named means this host's own, which is what the guest will boot as. The
    # largest-reserve fallback below is for an arch that *is* named and unknown -- a
    # guest whose overhead nobody measured, where over-reserving is the safe direction.
    # Reaching it for `None` instead made the two entry points quote different figures
    # for the same guest on any host that is not the arch that fallback came from: the
    # TUI's pricing page showed one overhead and the VM booted with another. It also
    # built the config prefix `...GUEST_KERNEL_RESERVE.None`, under which no operator
    # override could ever be found (issue #321).
    canonical = normalize_arch_tag(arch) or arch or host_arch_tag()
    default_mib, default_ratio = _DEFAULT_GUEST_KERNEL_RESERVE.get(
        canonical, _FALLBACK_GUEST_KERNEL_RESERVE
    )
    prefix = f"virtualizers.ch.GUEST_KERNEL_RESERVE.{canonical}"
    fixed_mib = max(0, _env_int(f"{prefix}.MIB", default_mib))
    ratio = max(0.0, _env_float(f"{prefix}.RATIO", default_ratio))
    return fixed_mib * 1024 * 1024, ratio


def guest_kernel_reserve_bytes(usable_bytes: int, arch: Optional[str] = None) -> int:
    """The overhead a guest of ``arch`` needs on top of ``usable_bytes``.

    Split out from :func:`guest_boot_memory_bytes` because the operator-facing side
    (the TUI's pricing page, `nodo`'s config docs) wants the overhead on its own: it
    is what the node absorbs and never bills, so it is what a memory price has to be
    set high enough to cover.
    """
    usable_bytes = int(usable_bytes)
    if usable_bytes <= 0:
        return 0
    fixed, ratio = _reserve_for_arch(arch)
    return fixed + int(math.ceil(usable_bytes * ratio))


def guest_kernel_reserve_table() -> Dict[str, Dict[str, float]]:
    """Every arch's reserve, for whoever has to show or quote it.

    Read live from config rather than captured at import, so the TUI and the node
    agree after an operator edits one.
    """
    table = {}
    for arch in _DEFAULT_GUEST_KERNEL_RESERVE:
        fixed, ratio = _reserve_for_arch(arch)
        table[arch] = {"fixed_bytes": fixed, "ratio": ratio}
    return table


def known_reserve_architectures() -> Tuple[str, ...]:
    """Arch tags nodo has a measured guest-kernel reserve for.

    The list the operator-facing surfaces enumerate -- the pricing page's per-arch
    rows, `config.example.yaml`'s commented block. Deliberately not
    `SUPPORTED_ARCHITECTURES`: that answers what this host can boot *right now*
    (emulator installed, guest kernel on disk), which is a different question from
    what nodo can quote an overhead for, and a price the operator set for an arch
    should not vanish from the editor because an emulator was uninstalled.
    """
    return tuple(_DEFAULT_GUEST_KERNEL_RESERVE)


def guest_boot_memory_bytes(usable_bytes: int, arch: Optional[str] = None) -> int:
    """Hypervisor allocation so a guest can really offer ``usable_bytes`` to userspace.

    ``arch`` is the canonical tag of the guest being booted (``linux/amd64``,
    ``linux/arm64``); omitted, the host's own arch is assumed, which is what a caller
    that has not resolved a service is booting. It selects the reserve, because the
    overhead is a property of the guest kernel, not of the node.

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
    never for the kernel underneath it. What that costs the node is exactly
    :func:`guest_kernel_reserve_bytes`, which the TUI's pricing page shows so the
    operator can price memory with it in view.
    """
    usable_bytes = int(usable_bytes)
    if usable_bytes <= 0:
        return usable_bytes
    # `arch` straight through: resolving an absent one is `_reserve_for_arch`'s job, so
    # that this and `guest_kernel_reserve_bytes` -- the figure the operator is shown --
    # cannot resolve it two different ways (issue #321).
    return usable_bytes + guest_kernel_reserve_bytes(usable_bytes, arch)


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

    # If disk_space is set in at_init, we use it directly as the requested size;
    # otherwise fall through to at_most. The unconditional `return` this used to
    # end each iteration with meant at_most was never reached at all: an unset
    # at_init reads back as disk_space=0, not an exception, so the first pass
    # returned None before the loop ever got to at_most.
    for scope_name in ("at_init", "at_most"):
        scope = getattr(resources, scope_name, None)
        if scope is None:
            continue
        try:
            value = int(getattr(scope, "disk_space", 0) or 0)
        except Exception:
            continue
        if value > 0:
            return value

    return None


def is_read_only_service(service: celaut_pb2.Service, filesystem=None) -> bool:
    """Whether this service declared ``read_mode=ro`` on its filesystem root.

    ``filesystem`` is the already-loaded tree, for the one caller that has it in
    hand (the build). Without it the field is read off the spec, which is only
    possible when the filesystem is inline: a service whose tree is stored as a
    block says nothing here rather than fetching the whole image to ask, and gets
    the writable floors. That is the safe direction -- it prices above what the
    image will cost, never below -- and the build, which does load the tree,
    resolves it exactly.
    """
    if filesystem is None:
        filesystem = _inline_filesystem(service)
        if filesystem is None:
            return False
    try:
        return read_mode(filesystem) == READ_MODE_RO
    except ValueError:
        # An unreadable read_mode is the build's error to raise, with the path and
        # the accepted values in it. Pricing does not get to refuse a manifest.
        return False


def _inline_filesystem(service: celaut_pb2.Service):
    """The Filesystem in ``container.filesystem``, when the bytes hold one inline.

    Stays inside this module's dependency budget: no block registry, no reads off
    disk. A pointer parses into a Filesystem with no branches and no xattrs, which
    answers "rw" -- the floor -- rather than a wrong "ro".
    """
    raw = getattr(getattr(service, "container", None), "filesystem", b"") or b""
    if not raw:
        return None
    filesystem = celaut_pb2.Service.Container.Filesystem()
    try:
        filesystem.ParseFromString(raw)
    except Exception:
        return None
    return filesystem


def initial_rootfs_size_bytes(
    service: celaut_pb2.Service,
    total_bytes: int,
    read_only: bool = False,
) -> int:
    """Size to format the rootfs image at, given the tree it has to hold.

    ``read_only`` inverts what the manifest's ``disk_space`` means, because the two
    modes want opposite things from it. A writable image has to hold writes that
    have not happened yet, so the declared figure is a floor and the image is grown
    to it. An immutable one holds exactly the tree it was built from and nothing
    will ever be added, so there is nothing to reserve and the declared figure is a
    CEILING instead -- checked by the caller against the populated tree (see
    ``assert_within_disk_space_ceiling``) and not used to size anything.

    So in ``ro`` this returns the tree's own size: the mkfs tool decides the image,
    and it will be smaller still once it compresses.
    """
    if read_only:
        return int(total_bytes)

    return max(
        MIN_ROOTFS_BYTES,
        int(total_bytes) + OVERHEAD_BYTES,
        int(requested_disk_space_bytes(service) or 0),
    )


def assert_within_disk_space_ceiling(service: celaut_pb2.Service, total_bytes: int) -> None:
    """Refuse a read-only build whose populated tree exceeds what it declared.

    The ``ro`` half of the ceiling semantics above. A service declaring 1 GiB and
    occupying 221 MiB is charged for 221 MiB; one declaring 1 GiB and occupying
    2 GiB is refused here, rather than silently built at 2 GiB and billed for it.

    A manifest that names no disk at all declares no ceiling, and passes.
    """
    ceiling = requested_disk_space_bytes(service)
    if not ceiling:
        return
    if int(total_bytes) > int(ceiling):
        raise ValueError(
            f"read-only rootfs tree is {int(total_bytes)} bytes, above the "
            f"{int(ceiling)} bytes this service declared as at_init.disk_space. "
            "For a read_mode=ro service that figure is a ceiling, not a floor: the "
            "image holds exactly the packed tree and is never grown to fit it."
        )


def billable_resources(
    declared: Optional[celaut_pb2.Sysresources],
    built_rootfs_size_bytes: Optional[int] = None,
    read_only: bool = False,
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

    ``read_only`` drops the floors for the one case where they describe nothing the
    instance holds. MIN_ROOTFS_BYTES and a declared ``disk_space`` are both promises
    of writable capacity, and a ``read_mode=ro`` instance has none: what it holds is
    its image, the whole of it, and never a byte more. So the built size IS the
    price, and pricing it at the floor would be charging a 12 MB capsule for
    128 MiB it was never given. Only meaningful together with
    ``built_rootfs_size_bytes`` -- the image has to exist for its size to be the
    answer -- so without one the floors stay.
    """
    _, mem_b, cpu_quota, cpu_period = resolve_initial_resources(declared)

    declared_disk = 0
    try:
        if declared is not None and declared.HasField("disk_space"):
            declared_disk = int(declared.disk_space)
    except Exception:
        declared_disk = 0

    if read_only and built_rootfs_size_bytes:
        disk_space = int(built_rootfs_size_bytes)
    else:
        disk_space = max(
            MIN_ROOTFS_BYTES, declared_disk, int(built_rootfs_size_bytes or 0)
        )

    return celaut_pb2.Sysresources(
        mem_limit=mem_b,
        cpu_period=cpu_period,
        cpu_quota=cpu_quota,
        disk_space=disk_space,
    )
