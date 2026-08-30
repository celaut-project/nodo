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

Disk has always been sized this way (``initial_rootfs_size_bytes`` adds
``OVERHEAD_BYTES`` so a service asking for N bytes can store N bytes). These tests
pin the same property for memory, and pin the separation that keeps it safe: the
*boot allocation* grows, the *billable figure* does not.
"""
import unittest

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.ch import limits
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    limits = None  # type: ignore[assignment]

MIB = 1024 * 1024

# Overhead measured on the guest kernel this node ships (Linux 6.12, arm64 virt),
# read from the boot line `Memory: <avail>K/<total>K available`.
MEASURED = {
    128 * MIB: 105172 * 1024,   # -m 128M -> 102.7 MiB usable
    256 * MIB: 233604 * 1024,   # -m 256M -> 228.1 MiB usable
}
# The two measurements fit a straight line, which is what the kernel's costs
# actually are: a fixed part (text, rodata, percpu, initramfs) plus a part
# proportional to the guest's size (one `struct page` per 4 KiB frame). Solving the
# pair gives ~2% per byte and ~22.7 MiB fixed. Modelling it this way rather than as
# a flat worst-case fraction matters: the fraction observed on the *smallest* guest
# (19.8%) is dominated by the fixed part and wildly overstates a large guest's cost.
_SIZES = sorted(MEASURED)
_OVERHEAD_RATIO = (
    (_SIZES[1] - MEASURED[_SIZES[1]]) - (_SIZES[0] - MEASURED[_SIZES[0]])
) / (_SIZES[1] - _SIZES[0])
_OVERHEAD_FIXED = (_SIZES[0] - MEASURED[_SIZES[0]]) - _OVERHEAD_RATIO * _SIZES[0]


def modelled_usable(boot_alloc):
    """What this guest kernel would leave userspace out of ``boot_alloc``."""
    return boot_alloc - (_OVERHEAD_FIXED + _OVERHEAD_RATIO * boot_alloc)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GuestBootMemoryTests(unittest.TestCase):

    def test_a_guest_is_booted_with_more_than_the_service_asked_to_use(self):
        self.assertGreater(limits.guest_boot_memory_bytes(256 * MIB), 256 * MIB)

    def test_the_headroom_covers_the_overhead_actually_measured(self):
        """If the reserve is smaller than the real overhead the bug is not fixed.

        The service would still be unable to allocate what it declared, and would
        still be OOM-killed below its own ceiling.
        """
        for boot_alloc, usable in MEASURED.items():
            overhead = boot_alloc - usable
            with self.subTest(boot_alloc=boot_alloc):
                self.assertGreaterEqual(
                    limits.guest_boot_memory_bytes(boot_alloc) - boot_alloc,
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
        boot = limits.guest_boot_memory_bytes(declared)
        self.assertGreaterEqual(modelled_usable(boot), declared)

    def test_every_size_gets_at_least_what_it_declared(self):
        """Not just the one size that failed live: the property has to hold generally."""
        for declared_mib in (128, 200, 256, 512, 1024, 4096, 16384):
            declared = declared_mib * MIB
            with self.subTest(declared_mib=declared_mib):
                boot = limits.guest_boot_memory_bytes(declared)
                self.assertGreaterEqual(modelled_usable(boot), declared)

    def test_the_model_reproduces_the_measurements_it_was_fitted_to(self):
        """Guard the model itself, so the assertions above rest on something real."""
        for boot_alloc, usable in MEASURED.items():
            with self.subTest(boot_alloc=boot_alloc):
                self.assertAlmostEqual(
                    modelled_usable(boot_alloc) / usable, 1.0, places=3
                )

    def test_the_overhead_grows_with_the_guest(self):
        """A fixed byte reserve would be wrong at both ends.

        `struct page` costs 64 bytes per 4 KiB frame, so a 16 GiB guest pays ~256 MiB
        for its page array alone -- a reserve that suits a 128 MiB guest cannot suit
        that one.
        """
        small = limits.guest_boot_memory_bytes(128 * MIB) - 128 * MIB
        large = limits.guest_boot_memory_bytes(16 * 1024 * MIB) - 16 * 1024 * MIB
        self.assertGreater(large, small)

    def test_nothing_is_added_to_nothing(self):
        self.assertEqual(limits.guest_boot_memory_bytes(0), 0)

    def test_a_negative_figure_is_returned_untouched(self):
        self.assertEqual(limits.guest_boot_memory_bytes(-1), -1)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheBillableFigureIsUnchangedTests(unittest.TestCase):
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
