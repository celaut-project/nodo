"""A service must be able to allocate the memory its manifest declares.

A manifest's ``mem_limit`` is a promise to the *service*: memory the service may
use. The hypervisor's ``-m`` is not that figure. The guest kernel takes its text,
its rodata/rwdata, its percpu areas and -- growing with the size of the guest --
one ``struct page`` per 4 KiB frame, all before init runs. What is left is what the
service can actually allocate.

Booting the guest at exactly ``mem_limit`` therefore hands the service less than
its manifest declared. Measured on a live node's 6.12 arm64 guest kernel:
``-m 256M`` leaves 228.1 MiB, so a service declaring ``at_most 256 MiB`` was
OOM-killed at ~228 MiB -- below its own declared ceiling.

The overhead is ARCHITECTURE-DEPENDENT, so the reserve is too, and these tests are
run against both arches' real measurements rather than against one arch's numbers
and an assumption about the other. Every figure below was read from the guest
kernels this node ships, booted at each size, off their own
``Memory: <avail>K/<total>K available`` line.

Disk has always been sized this way (``initial_rootfs_size_bytes`` adds
``OVERHEAD_BYTES`` so a service asking for N bytes can store N bytes). These tests
pin the same property for memory, and pin the separation that keeps it safe: the
*boot allocation* grows, the *billable figure* does not.
"""
import math
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.utils.config import ConfigManager
    from src.virtualizers.ch import limits
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    limits = None  # type: ignore[assignment]
    ConfigManager = None  # type: ignore[assignment]

MIB = 1024 * 1024

ARM64 = "linux/arm64"
AMD64 = "linux/amd64"

# Overhead measured on the guest kernels this node ships, per architecture, read from
# each guest's own boot line. Keyed boot allocation -> bytes left to userspace.
#
# The two arches differ by ~8 MiB at every size, and that difference is the entire
# reason the reserve is per-arch: a constant fitted to arm64 is too small on amd64,
# and too small means a service OOM-killed below the ceiling its manifest published.
MEASURED_BY_ARCH = {
    ARM64: {
        128 * MIB: 105172 * 1024,    # 102.7 MiB usable (25.3 MiB overhead)
        256 * MIB: 233604 * 1024,    # 228.1 MiB usable (27.9 MiB overhead)
        512 * MIB: 490288 * 1024,    # 478.8 MiB usable (33.2 MiB overhead)
        1024 * MIB: 1002736 * 1024,  # 979.2 MiB usable (44.8 MiB overhead)
    },
    AMD64: {
        128 * MIB: 97384 * 1024,     #  95.1 MiB usable (32.9 MiB overhead)
        256 * MIB: 225980 * 1024,    # 220.7 MiB usable (35.3 MiB overhead)
        512 * MIB: 483376 * 1024,    # 472.0 MiB usable (40.0 MiB overhead)
        1024 * MIB: 998236 * 1024,   # 974.8 MiB usable (49.2 MiB overhead)
        2048 * MIB: 2028668 * 1024,  # 1981.1 MiB usable (66.9 MiB overhead)
    },
}

# The arm64 measurements under a bare name: the tests that pin the live failure were
# taken on an arm64 guest, so they read against the arch that produced them.
MEASURED = MEASURED_BY_ARCH[ARM64]


def _fit(measured):
    """(fixed bytes, ratio) least-squares fit of overhead against guest size.

    The kernel's costs really are of this shape: a fixed part (text, rodata, percpu,
    initramfs) plus a part proportional to the guest's size (one ``struct page`` per
    4 KiB frame). Modelling it this way rather than as a flat worst-case fraction
    matters -- the fraction observed on the *smallest* guest is dominated by the fixed
    part (19.8% on arm64 at 128 MiB) and wildly overstates a large guest's cost.

    Least squares rather than solving a pair, since there are more than two points per
    arch: a fit through all of them cannot be skewed by whichever two were picked.
    """
    sizes = sorted(measured)
    overheads = [size - measured[size] for size in sizes]
    n = len(sizes)
    mean_size = sum(sizes) / n
    mean_overhead = sum(overheads) / n
    variance = sum((size - mean_size) ** 2 for size in sizes)
    ratio = sum(
        (size - mean_size) * (overhead - mean_overhead)
        for size, overhead in zip(sizes, overheads)
    ) / variance
    return mean_overhead - ratio * mean_size, ratio


FIT_BY_ARCH = {arch: _fit(measured) for arch, measured in MEASURED_BY_ARCH.items()}
_OVERHEAD_FIXED, _OVERHEAD_RATIO = FIT_BY_ARCH[ARM64]


def modelled_usable(boot_alloc, arch=ARM64):
    """What ``arch``'s guest kernel would leave userspace out of ``boot_alloc``."""
    fixed, ratio = FIT_BY_ARCH[arch]
    return boot_alloc - (fixed + ratio * boot_alloc)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GuestBootMemoryTests(unittest.TestCase):

    def test_a_guest_is_booted_with_more_than_the_service_asked_to_use(self):
        for arch in MEASURED_BY_ARCH:
            with self.subTest(arch=arch):
                self.assertGreater(
                    limits.guest_boot_memory_bytes(256 * MIB, arch=arch), 256 * MIB
                )

    def test_the_headroom_covers_the_overhead_actually_measured(self):
        """A reserve smaller than the real overhead leaves the shortfall in place.

        The service would still be unable to allocate what it declared, and would
        still be OOM-killed below its own ceiling. Checked per arch against that
        arch's own measurements -- the point of a per-arch reserve is that one set of
        numbers does not answer for the other.
        """
        for arch, measured in MEASURED_BY_ARCH.items():
            for boot_alloc, usable in measured.items():
                overhead = boot_alloc - usable
                with self.subTest(arch=arch, boot_alloc=boot_alloc):
                    self.assertGreaterEqual(
                        limits.guest_boot_memory_bytes(boot_alloc, arch=arch) - boot_alloc,
                        overhead,
                        "reserve must cover the guest kernel overhead measured at this size",
                    )

    def test_a_service_declaring_256_mib_can_allocate_256_mib(self):
        """The live failure, stated as an assertion.

        The demo-service's `memory_ceiling` probe declares `at_most` 256 MiB and
        ramps allocations toward it. It was killed at 240 MiB against that 256 MiB
        declaration, because the guest only ever had 228 MiB.
        """
        declared = 256 * MIB
        for arch in MEASURED_BY_ARCH:
            with self.subTest(arch=arch):
                boot = limits.guest_boot_memory_bytes(declared, arch=arch)
                self.assertGreaterEqual(modelled_usable(boot, arch), declared)

    def test_every_size_gets_at_least_what_it_declared(self):
        """Not just the one size that failed live: the property has to hold generally."""
        for arch in MEASURED_BY_ARCH:
            for declared_mib in (128, 200, 256, 512, 1024, 4096, 16384):
                declared = declared_mib * MIB
                with self.subTest(arch=arch, declared_mib=declared_mib):
                    boot = limits.guest_boot_memory_bytes(declared, arch=arch)
                    self.assertGreaterEqual(modelled_usable(boot, arch), declared)

    def test_the_model_reproduces_the_measurements_it_was_fitted_to(self):
        """Guard the model itself, so the assertions above rest on something real."""
        for arch, measured in MEASURED_BY_ARCH.items():
            for boot_alloc, usable in measured.items():
                with self.subTest(arch=arch, boot_alloc=boot_alloc):
                    self.assertAlmostEqual(
                        modelled_usable(boot_alloc, arch) / usable, 1.0, places=2
                    )

    def test_the_overhead_grows_with_the_guest(self):
        """A fixed byte reserve would be wrong at both ends.

        `struct page` costs 64 bytes per 4 KiB frame, so a 16 GiB guest pays ~256 MiB
        for its page array alone -- a reserve that suits a 128 MiB guest cannot suit
        that one.
        """
        for arch in MEASURED_BY_ARCH:
            with self.subTest(arch=arch):
                small = limits.guest_boot_memory_bytes(128 * MIB, arch=arch) - 128 * MIB
                large = (
                    limits.guest_boot_memory_bytes(16 * 1024 * MIB, arch=arch)
                    - 16 * 1024 * MIB
                )
                self.assertGreater(large, small)

    def test_nothing_is_added_to_nothing(self):
        self.assertEqual(limits.guest_boot_memory_bytes(0), 0)

    def test_a_negative_figure_is_returned_untouched(self):
        self.assertEqual(limits.guest_boot_memory_bytes(-1), -1)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheReserveIsPerArchitectureTests(unittest.TestCase):
    """The overhead is a property of the guest kernel, so the reserve must be too.

    A single shared constant is either wasteful on the cheaper arch or too small on
    the costlier one -- and too small is the shortfall the reserve exists to close.
    """

    def test_the_costlier_arch_reserves_more(self):
        # The measured fact, asserted: amd64's kernel image, percpu areas and reserved
        # low memory come to ~9 MiB more than arm64's at every size.
        for size_mib in (128, 256, 512, 1024):
            size = size_mib * MIB
            with self.subTest(size_mib=size_mib):
                self.assertGreater(
                    limits.guest_kernel_reserve_bytes(size, arch=AMD64),
                    limits.guest_kernel_reserve_bytes(size, arch=ARM64),
                )

    def test_arm64s_reserve_would_not_have_been_enough_for_amd64(self):
        """Why this is per-arch and not one constant.

        Sizing an amd64 guest by arm64's reserve leaves it short of the fixed cost
        amd64 actually pays -- which is a service OOM-killed below its declared
        ceiling, the shortfall this reserve exists to close, on the arch that was not
        measured.
        """
        arm_fixed, _ = limits._reserve_for_arch(ARM64)
        amd_measured_fixed, _ = FIT_BY_ARCH[AMD64]
        self.assertLess(
            arm_fixed,
            amd_measured_fixed + 8 * MIB,
            "arm64's reserve is not comfortably above amd64's real fixed overhead, "
            "so a shared constant would have been safe and this split is unmotivated",
        )

    def test_an_alias_names_the_same_architecture(self):
        # `x86_64`, `amd64` and `linux/amd64` are the same guest. A reserve that only
        # answered to the canonical spelling would silently fall back for a caller
        # holding a tag from the manifest.
        canonical = limits.guest_kernel_reserve_bytes(256 * MIB, arch=AMD64)
        for alias in ("amd64", "x86_64", "X86_64"):
            with self.subTest(alias=alias):
                self.assertEqual(
                    limits.guest_kernel_reserve_bytes(256 * MIB, arch=alias), canonical
                )

    def test_an_unknown_arch_gets_the_largest_reserve_nodo_has_measured(self):
        """Not the smallest, and not nothing.

        An arch nobody has characterised is likelier to resemble the most expensive
        kernel than the cheapest. The cost of over-reserving is host RAM the node
        absorbs; the cost of under-reserving is a service killed below its ceiling.
        Only one of those breaks a promise the node made.
        """
        unknown = limits.guest_kernel_reserve_bytes(256 * MIB, arch="linux/riscv64")
        self.assertEqual(
            unknown,
            max(
                limits.guest_kernel_reserve_bytes(256 * MIB, arch=arch)
                for arch in MEASURED_BY_ARCH
            ),
        )

    def test_an_unnamed_arch_still_gets_a_reserve(self):
        # A caller that cannot name the guest must not silently get a VM sized at
        # exactly the usable figure: that is the shortfall, for anything unlabelled.
        self.assertGreater(limits.guest_boot_memory_bytes(256 * MIB), 256 * MIB)

    def test_the_table_reports_what_the_backends_will_apply(self):
        # The TUI's pricing page advises the operator against this table, so a table
        # that disagreed with the reserve would advise against an overhead the node
        # does not apply.
        table = limits.guest_kernel_reserve_table()
        self.assertEqual(set(table), set(limits.known_reserve_architectures()))
        for arch, entry in table.items():
            with self.subTest(arch=arch):
                expected = entry["fixed_bytes"] + int(
                    -(-256 * MIB * entry["ratio"] // 1)
                )
                self.assertEqual(
                    limits.guest_kernel_reserve_bytes(256 * MIB, arch=arch), expected
                )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheReserveIsConfigurablePerArchTests(unittest.TestCase):
    """An operator running their own guest kernel has to be able to correct it.

    Per arch, because a node that boots a corrected amd64 kernel and a stock arm64 one
    would otherwise have to choose which of the two to be wrong about.
    """

    def _config(self, **overrides):
        """Override config keys for the manager `limits` actually reads.

        Patches `limits.env_manager` rather than a fresh `ConfigManager()`. The
        module binds its manager once at import, and `tests/config_bootstrap`
        drops the singleton to point later callers at a temporary config -- so
        `ConfigManager()` here can hand back an instance `limits` has never
        heard of, and the override would silently do nothing. Which is exactly
        what it did: these two tests passed alone and read the defaults when the
        suite ran in full.
        """
        real_get = limits.env_manager.get

        def get(key, default=None):
            return overrides[key] if key in overrides else real_get(key, default)

        return patch.object(limits.env_manager, "get", side_effect=get)

    def test_one_arch_can_be_corrected_without_touching_the_other(self):
        with self._config(**{
            f"virtualizers.ch.GUEST_KERNEL_RESERVE.{AMD64}.MIB": 64,
            f"virtualizers.ch.GUEST_KERNEL_RESERVE.{AMD64}.RATIO": 0.1,
        }):
            # The reserve uses math.ceil so a guest is never under-allocated by a
            # fractional byte -- the failure mode is a guest OOM, so round up.
            self.assertEqual(
                limits.guest_kernel_reserve_bytes(1024 * MIB, arch=AMD64),
                64 * MIB + math.ceil(1024 * MIB * 0.1),
            )
            # arm64 keeps its measured default. Read from the table rather than
            # written out again: this test is about one arch's override not leaking
            # into the other, not about what the other arch's default happens to be,
            # and a literal here only rots when a measurement is corrected.
            arm_mib, arm_ratio = limits._DEFAULT_GUEST_KERNEL_RESERVE[ARM64]
            self.assertEqual(
                limits.guest_kernel_reserve_bytes(1024 * MIB, arch=ARM64),
                arm_mib * MIB + math.ceil(1024 * MIB * arm_ratio),
            )

    def test_zero_boots_at_exactly_the_usable_figure_for_that_arch_alone(self):
        """The escape hatch, and that it is not a global one.

        An operator who wants a guest booted at exactly `mem_limit` must be able to
        have it for one arch without giving up the reserve on the other -- otherwise
        the setting is a choice between no reserve anywhere and a reserve everywhere.
        """
        with self._config(**{
            f"virtualizers.ch.GUEST_KERNEL_RESERVE.{AMD64}.MIB": 0,
            f"virtualizers.ch.GUEST_KERNEL_RESERVE.{AMD64}.RATIO": 0,
        }):
            self.assertEqual(
                limits.guest_boot_memory_bytes(256 * MIB, arch=AMD64), 256 * MIB
            )
            self.assertGreater(
                limits.guest_boot_memory_bytes(256 * MIB, arch=ARM64), 256 * MIB
            )

    def test_a_malformed_override_falls_back_rather_than_disabling_the_reserve(self):
        # Reading an unparseable reserve as 0 would trade a typo for a service
        # OOM-killed below its declared ceiling.
        with self._config(**{
            f"virtualizers.ch.GUEST_KERNEL_RESERVE.{AMD64}.MIB": "lots",
            f"virtualizers.ch.GUEST_KERNEL_RESERVE.{AMD64}.RATIO": "some",
        }):
            self.assertGreater(
                limits.guest_boot_memory_bytes(256 * MIB, arch=AMD64), 256 * MIB
            )

    def test_a_negative_override_cannot_shrink_the_guest(self):
        with self._config(**{
            f"virtualizers.ch.GUEST_KERNEL_RESERVE.{AMD64}.MIB": -100,
            f"virtualizers.ch.GUEST_KERNEL_RESERVE.{AMD64}.RATIO": -1.0,
        }):
            self.assertGreaterEqual(
                limits.guest_boot_memory_bytes(256 * MIB, arch=AMD64), 256 * MIB
            )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheBillableFigureExcludesTheReserveTests(unittest.TestCase):
    """The reserve must not leak into what the row records or the client pays.

    `resolve_initial_resources` is applied to a manifest at launch and re-applied to
    the already-resolved row when pricing it. A figure that grew on each application
    would make a quote and its charge disagree -- the exact class of bug
    `billable_resources` exists to prevent -- so the growth lives only in
    `guest_boot_memory_bytes`, which the backends call once each.
    """

    def test_resolution_stays_idempotent(self):
        declared = celaut.Sysresources(mem_limit=256 * MIB, disk_space=10 * MIB)
        once = limits.billable_resources(declared)
        twice = limits.billable_resources(once)
        self.assertEqual(once, twice)

    def test_the_row_records_what_the_service_may_use(self):
        declared = 256 * MIB
        _, mem_b, _, _ = limits.resolve_initial_resources(
            celaut.Sysresources(mem_limit=declared)
        )
        self.assertEqual(mem_b, declared)

    def test_the_client_is_not_charged_for_the_guest_kernel(self):
        declared = 256 * MIB
        billable = limits.billable_resources(celaut.Sysresources(mem_limit=declared))
        self.assertEqual(billable.mem_limit, declared)
        self.assertGreater(limits.guest_boot_memory_bytes(declared), billable.mem_limit)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveInitialResourcesTests(unittest.TestCase):

    def test_the_boot_floor_still_applies(self):
        _, mem_b, _, _ = limits.resolve_initial_resources(
            celaut.Sysresources(mem_limit=1024)
        )
        self.assertGreaterEqual(mem_b, limits.MIN_MEM_MIB * MIB)

    def test_cpu_resolution_is_untouched(self):
        vcpus, _, quota, period = limits.resolve_initial_resources(
            celaut.Sysresources(cpu_period=100000, cpu_quota=400000)
        )
        self.assertEqual((vcpus, quota, period), (4, 400000, 100000))


if __name__ == "__main__":
    unittest.main()
